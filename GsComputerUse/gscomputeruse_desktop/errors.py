class ComputerNotReady(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ComputerBusy(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
