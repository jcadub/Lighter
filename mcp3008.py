import spidev

# LPMPCB1 port mapping: port number -> (chip, channel)
# Chip 0 = SPI bus 0, CE0: voltage reference on ch0, CT ports 9-15 on ch1-7
# Chip 1 = SPI bus 0, CE1: CT ports 1-8 on ch0-7
PORT_MAP = {
    1:  (1, 0),
    2:  (1, 1),
    3:  (1, 2),
    4:  (1, 3),
    5:  (1, 4),
    6:  (1, 5),
    7:  (1, 6),
    8:  (1, 7),
    9:  (0, 1),
    10: (0, 2),
    11: (0, 3),
    12: (0, 4),
    13: (0, 5),
    14: (0, 6),
    15: (0, 7),
}

VOLTAGE_CHIP = 0
VOLTAGE_CHANNEL = 0
SPI_SPEED_HZ = 810000


class MCP3008:
    """Driver for a single MCP3008 10-bit SPI ADC."""

    def __init__(self, bus, device, speed_hz=SPI_SPEED_HZ):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

    def read(self, channel):
        # Matches Java: pins[p] = new byte[]{1, (byte)((p + 8) << 4), 0}
        # Result extracted from bytes 1-2: ((resp[1] & 0x03) << 8) | (resp[2] & 0xFF)
        resp = self.spi.xfer2([1, (channel + 8) << 4, 0])
        return ((resp[1] & 0x03) << 8) | (resp[2] & 0xFF)

    def close(self):
        self.spi.close()


class ADCReader:
    """Manages both MCP3008 chips for LPMPCB1 and exposes voltage/current reads."""

    def __init__(self, speed_hz=SPI_SPEED_HZ):
        self.chip0 = MCP3008(0, 0, speed_hz)
        self.chip1 = MCP3008(0, 1, speed_hz)
        self._chips = [self.chip0, self.chip1]

    def read_voltage(self):
        return self.chip0.read(VOLTAGE_CHANNEL)

    def read_current(self, port):
        chip_idx, channel = PORT_MAP[port]
        return self._chips[chip_idx].read(channel)

    def close(self):
        self.chip0.close()
        self.chip1.close()
