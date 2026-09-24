class SafetyError(Exception):
    """A machine-readable, secret-free reason to stop the pipeline."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
