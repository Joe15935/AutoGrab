class AutoGrabError(Exception):
    """Only safe, fixed diagnostic codes cross logging/CLI boundaries."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)
