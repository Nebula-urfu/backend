class Problem(Exception):
    def __init__(self, status, code, title):
        self.status = status
        self.code = code
        self.title = title


def missing():
    return Problem(404, "NOT_FOUND", "Объект не найден")


def invalid(message):
    return Problem(400, "VALIDATION_ERROR", message)


def require(condition):
    if not condition:
        raise Problem(409, "STEP_NOT_ALLOWED", "Действие недоступно на текущем этапе")


def version(actual, expected):
    if actual != expected:
        raise Problem(409, "STALE_VERSION", "Версия изменилась")
