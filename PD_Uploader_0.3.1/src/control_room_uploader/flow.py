class PreparationInterrupted(Exception):
    pass

class ReauthenticationNeeded(Exception):
    pass

class TargetMismatch(Exception):
    pass

class OutcomeUnknown(Exception):
    pass

class UploadRejected(Exception):
    """The site reported an error, but a partial server-side effect is not ruled out."""
