"""Error type with a stable machine-readable code (used for CLI JSON, exit codes and MCP results)."""

EXIT_CODES = {
    'INVALID_ARG': 2,        # bad option or value, value outside the device range
    'NOT_FOUND': 3,          # no PP300S answering on any USB serial port / the named port does not exist
    'BUSY': 4,               # the port is held by another program (vendor app, terminal) or another command
    'NOT_APPLIED': 5,        # the supply acknowledged but the readback shows no effect (e.g. wrong screen)
    'NO_REPLY': 6,           # no answer: wrong port, supply off, supply in its bootloader
    'BAD_REPLY': 6,          # an answer with a bad header, tail or checksum
    'PORT_ERROR': 6,         # serial I/O error (cable unplugged, driver error)
    'UNSAFE_OPERATION': 7,   # refused: over the bench limits, would trip a protection, or an unknown register
    'STOPPED': 0,
}


class PP300SError(Exception):
    def __init__(self, code, message, hint=None, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra or {}

    @property
    def exit_code(self):
        return EXIT_CODES.get(self.code, 1)

    def to_dict(self):
        d = {'code': self.code, 'message': self.message}
        if self.hint:
            d['hint'] = self.hint
        d.update(self.extra)
        return d
