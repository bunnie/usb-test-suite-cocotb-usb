import cocotb
from cocotb.triggers import RisingEdge
from cocotb.result import TestFailure, ReturnValue
from cocotb.utils import get_sim_time

from cocotb_usb.usb.pid import PID
from cocotb_usb.usb.endpoint import EndpointType, EndpointResponse
from cocotb_usb.usb.packet import crc16

from cocotb_usb.utils import grouper_tofit, parse_csr, assertEqual

from cocotb_usb.host import UsbTest


class UsbTestValenty(UsbTest):
    """Class for testing ValentyUSB IP core.
    Includes functions to communicate and generate responses without a CPU,
    making use of a Wishbone bridge.

    Args:
        dut : Object under test as passed by cocotb.
        csr_file (str): Path to a CSV file containing CSR register addresses,
            generated by Litex.
        decouple_clocks (bool, optional): Indicates whether host and device
            share clock signal. If set to False, you must provide clk48_device
            clock in test.
    """
    def __init__(self, dut, csr_file, **kwargs):
        # Litex imports
        from cocotb_usb.wishbone import WishboneMaster

        self.wb = WishboneMaster(dut, "wishbone", dut.clk12, timeout=20)
        self.csrs = dict()
        self.csrs = parse_csr(csr_file)
        super().__init__(dut, **kwargs)

    @cocotb.coroutine
    def reset(self):
        yield super().reset()

        # Enable endpoint 0
        yield self.write(self.csrs['usb_setup_ev_enable'], 0xff)
        yield self.write(self.csrs['usb_in_ev_enable'], 0xff)
        yield self.write(self.csrs['usb_out_ev_enable'], 0xff)

        yield self.write(self.csrs['usb_setup_ev_pending'], 0xff)
        yield self.write(self.csrs['usb_in_ev_pending'], 0xff)
        yield self.write(self.csrs['usb_out_ev_pending'], 0xff)
        yield self.write(self.csrs['usb_address'], 0)

    @cocotb.coroutine
    def write(self, addr, val):
        yield self.wb.write(addr, val)

    @cocotb.coroutine
    def read(self, addr):
        value = yield self.wb.read(addr)
        raise ReturnValue(value)

    @cocotb.coroutine
    def connect(self):
        USB_PULLUP_OUT = self.csrs['usb_pullup_out']
        yield self.write(USB_PULLUP_OUT, 1)

    @cocotb.coroutine
    def clear_pending(self, epaddr):
        if EndpointType.epdir(epaddr) == EndpointType.IN:
            # Reset endpoint
            self.dut._log.info("Clearing IN_EV_PENDING")
            yield self.write(self.csrs['usb_in_ctrl'], 0x20)
            yield self.write(self.csrs['usb_in_ev_pending'], 0xff)
        else:
            self.dut._log.info("Clearing OUT_EV_PENDING")
            yield self.write(self.csrs['usb_out_ev_pending'], 0xff)
            yield self.write(self.csrs['usb_out_ctrl'], 0x20)

    @cocotb.coroutine
    def disconnect(self):
        super().disconnect()
        USB_PULLUP_OUT = self.csrs['usb_pullup_out']
        self.address = 0
        yield self.write(USB_PULLUP_OUT, 0)

    @cocotb.coroutine
    def pending(self, ep):
        if EndpointType.epdir(ep) == EndpointType.IN:
            val = yield self.read(self.csrs['usb_in_status'])
            raise ReturnValue(val & (1 << 4))
        else:
            val = yield self.read(self.csrs['usb_out_status'])
            raise ReturnValue((val & (1 << 5) | (1 << 4))
                              and (EndpointType.epnum(ep) == (val & 0x0f)))

    @cocotb.coroutine
    def expect_setup(self, epaddr, expected_data):
        actual_data = []
        # wait for data to appear
        for i in range(128):
            self.dut._log.debug("Prime loop {}".format(i))
            status = yield self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if have:
                break
            yield RisingEdge(self.dut.clk12)

        for i in range(48):
            self.dut._log.debug("Read loop {}".format(i))
            status = yield self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if not have:
                break
            v = yield self.read(self.csrs['usb_setup_data'])
            actual_data.append(v)
            yield RisingEdge(self.dut.clk12)

        if len(actual_data) < 2:
            raise TestFailure("data was short (got {}, expected {})".format(
                expected_data, actual_data))
        actual_data, actual_crc16 = actual_data[:-2], actual_data[-2:]

        self.print_ep(epaddr, "Got: %r (expected: %r)", actual_data,
                      expected_data)
        assertEqual(expected_data, actual_data,
                    "SETUP packet not received")
        assertEqual(crc16(expected_data), actual_crc16,
                    "CRC16 not valid")
        # Acknowledge that we've handled the setup packet
        yield self.write(self.csrs['usb_setup_ctrl'], 2)

    @cocotb.coroutine
    def drain_setup(self):
        actual_data = []
        for i in range(48):
            status = yield self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if not have:
                break
            v = yield self.read(self.csrs['usb_setup_data'])
            actual_data.append(v)
            yield RisingEdge(self.dut.clk12)
        yield self.write(self.csrs['usb_setup_ctrl'], 2)
        # Drain the pending bit
        yield self.write(self.csrs['usb_setup_ev_pending'], 0xff)
        return actual_data

    @cocotb.coroutine
    def drain_out(self):
        actual_data = []
        for i in range(70):
            status = yield self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if not have:
                break
            v = yield self.read(self.csrs['usb_out_data'])
            actual_data.append(v)
            yield RisingEdge(self.dut.clk12)
        yield self.write(self.csrs['usb_out_ev_pending'], 0xff)
        yield self.write(self.csrs['usb_out_ctrl'], 0x10)
        return actual_data[:-2]  # Strip off CRC16

    @cocotb.coroutine
    def expect_data(self, epaddr, expected_data, expected):
        actual_data = []
        # wait for data to appear
        for i in range(128):
            self.dut._log.debug("Prime loop {}".format(i))
            status = yield self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if have:
                break
            yield RisingEdge(self.dut.clk12)

        for i in range(256):
            self.dut._log.debug("Read loop {}".format(i))
            status = yield self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if not have:
                break
            v = yield self.read(self.csrs['usb_out_data'])
            actual_data.append(v)
            yield RisingEdge(self.dut.clk12)

        if expected == PID.ACK:
            if len(actual_data) < 2:
                raise TestFailure("data {} was short".format(actual_data))
            actual_data, actual_crc16 = actual_data[:-2], actual_data[-2:]

            self.print_ep(epaddr, "Got: %r (expected: %r)", actual_data,
                          expected_data)
            assertEqual(expected_data, actual_data,
                        "DATA packet not correctly received")
            assertEqual(crc16(expected_data), actual_crc16,
                        "CRC16 not valid")
            pending = yield self.read(self.csrs['usb_out_ev_pending'])
            if pending != 1:
                raise TestFailure('event not generated')
            yield self.write(self.csrs['usb_out_ev_pending'], pending)

    @cocotb.coroutine
    def set_response(self, ep, response):
        if (EndpointType.epdir(ep) == EndpointType.IN
                and response == EndpointResponse.ACK):
            yield self.write(self.csrs['usb_in_ctrl'], EndpointType.epnum(ep))
        elif (EndpointType.epdir(ep) == EndpointType.OUT
                and response == EndpointResponse.ACK):
            yield self.write(self.csrs['usb_out_ctrl'],
                             0x10 | EndpointType.epnum(ep))

    @cocotb.coroutine
    def send_data(self, token, ep, data):
        for b in data:
            yield self.write(self.csrs['usb_in_data'], b)
        yield self.write(self.csrs['usb_in_ctrl'],
                         EndpointType.epnum(ep) & 0x0f)

    @cocotb.coroutine
    def transaction_setup(self, addr, data, epnum=0):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)

        xmit = cocotb.fork(self.host_setup(addr, epnum, data))
        yield self.expect_setup(epaddr_out, data)
        yield xmit.join()

    @cocotb.coroutine
    def transaction_data_out(self,
                             addr,
                             ep,
                             data,
                             chunk_size=64,
                             expected=PID.ACK,
                             datax=PID.DATA1):
        epnum = EndpointType.epnum(ep)

        # # Set it up so we ACK the final IN packet
        # yield self.write(self.csrs['usb_in_ctrl'], 0)
        for _i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            self.dut._log.warning("Sending {} bytes to host"
                                  .format(len(chunk)))
            self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
            # Enable receiving data
            yield self.set_response(ep, EndpointResponse.ACK)
            xmit = cocotb.fork(
                self.host_send(datax, addr, epnum, chunk, expected))
            yield self.expect_data(epnum, list(chunk), expected)
            yield xmit.join()

            if datax == PID.DATA0:
                datax = PID.DATA1
            else:
                datax = PID.DATA0

    @cocotb.coroutine
    def transaction_data_in(self,
                            addr,
                            ep,
                            data,
                            chunk_size=64,
                            datax=PID.DATA1):
        epnum = EndpointType.epnum(ep)
        sent_data = 0
        for i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            # Do we still have time?
            current = get_sim_time("us")
            if current > self.request_deadline:
                raise TestFailure("Failed to get all data in time")

            self.dut._log.debug("Expecting chunk {}".format(i))
            self.packet_deadline = current + 5e2  # 500 ms

            sent_data = 1
            self.dut._log.debug(
                "Actual data we're expecting: {}".format(chunk))
            for b in chunk:
                yield self.write(self.csrs['usb_in_data'], b)
            yield self.write(self.csrs['usb_in_ctrl'], epnum)
            recv = cocotb.fork(self.host_recv(datax, addr, epnum, chunk))
            yield recv.join()

            if datax == PID.DATA0:
                datax = PID.DATA1
            else:
                datax = PID.DATA0
        if not sent_data:
            yield self.write(self.csrs['usb_in_ctrl'], epnum)
            recv = cocotb.fork(self.host_recv(datax, addr, epnum, []))
            yield self.send_data(datax, epnum, data)
            yield recv.join()

    @cocotb.coroutine
    def set_data(self, ep, data):
        for b in data:
            yield self.write(self.csrs['usb_in_data'], b)

    @cocotb.coroutine
    def control_transfer_out(self, addr, setup_data, descriptor_data=None):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x80:
            raise Exception(
                "setup_data indicated an IN transfer, but you requested"
                "an OUT transfer"
            )

        setup_ev = yield self.read(self.csrs['usb_setup_ev_pending'])

        # Setup stage
        self.dut._log.info("setup stage")
        yield self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + super().MAX_REQUEST_TIME

        setup_ev = yield self.read(self.csrs['usb_setup_ev_pending'])
        yield self.write(self.csrs['usb_setup_ev_pending'], setup_ev)

        # Data stage
        if (setup_data[7] != 0
                or setup_data[6] != 0) and descriptor_data is None:
            raise Exception(
                "setup_data indicates data, but no descriptor data"
                "was specified"
            )
        if (setup_data[7] == 0
                and setup_data[6] == 0) and descriptor_data is not None:
            raise Exception(
                "setup_data indicates no data, but descriptor data"
                "was specified"
            )
        if descriptor_data is not None:
            self.dut._log.info("data stage")
            yield self.transaction_data_out(addr, epaddr_out, descriptor_data)

        # Status stage
        self.dut._log.info("status stage")
        self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
        yield self.write(self.csrs['usb_in_ctrl'], 0)  # Send empty IN packet
        yield self.transaction_status_in(addr, epaddr_in)
        yield RisingEdge(self.dut.clk12)
        yield RisingEdge(self.dut.clk12)
        in_ev = yield self.read(self.csrs['usb_in_ev_pending'])
        yield self.write(self.csrs['usb_in_ev_pending'], in_ev)
        yield self.write(self.csrs['usb_in_ctrl'], 1 << 5)  # Reset IN buffer

        # Was the time limit honored?
        if get_sim_time("us") > self.request_deadline:
            raise TestFailure("Failed to process the OUT request in time")

    @cocotb.coroutine
    def control_transfer_in(self, addr, setup_data, descriptor_data=None):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x00:
            raise Exception(
                "setup_data indicated an OUT transfer, but you requested"
                "an IN transfer"
            )

        setup_ev = yield self.read(self.csrs['usb_setup_ev_pending'])

        # Setup stage
        self.dut._log.info("setup stage")
        yield self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + super().MAX_REQUEST_TIME

        setup_ev = yield self.read(self.csrs['usb_setup_ev_pending'])
        yield self.write(self.csrs['usb_setup_ev_pending'], setup_ev)

        # Data stage
        in_ev = yield self.read(self.csrs['usb_in_ev_pending'])
        if (setup_data[7] != 0
                or setup_data[6] != 0) and descriptor_data is None:
            raise Exception(
                "setup_data indicates data, but no descriptor data"
                "was specified"
            )
        if (setup_data[7] == 0
                and setup_data[6] == 0) and descriptor_data is not None:
            raise Exception(
                "setup_data indicates no data, but descriptor data"
                "was specified"
            )
        if descriptor_data is not None:
            self.dut._log.info("data stage")
            yield self.transaction_data_in(addr, epaddr_in, descriptor_data)

            # Give the signal two clock cycles
            # to percolate through the event manager
            yield RisingEdge(self.dut.clk12)
            yield RisingEdge(self.dut.clk12)
            in_ev = yield self.read(self.csrs['usb_in_ev_pending'])
            yield self.write(self.csrs['usb_in_ev_pending'], in_ev)

        # Status stage
        self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
        yield self.write(self.csrs['usb_out_ctrl'], 0x10)  # Send empty packet
        self.dut._log.info("status stage")
        out_ev = yield self.read(self.csrs['usb_out_ev_pending'])
        yield self.transaction_status_out(addr, epaddr_out)
        yield RisingEdge(self.dut.clk12)
        out_ev = yield self.read(self.csrs['usb_out_ev_pending'])
        yield self.write(self.csrs['usb_out_ctrl'], 0x20)  # Reset FIFO
        yield self.write(self.csrs['usb_out_ev_pending'], out_ev)

        # Was the time limit honored?
        if get_sim_time("us") > self.request_deadline:
            raise TestFailure("Failed to process the IN request in time")

    @cocotb.coroutine
    def set_device_address(self, address):
        yield super().set_device_address(address)
        yield self.write(self.csrs['usb_address'], address)
