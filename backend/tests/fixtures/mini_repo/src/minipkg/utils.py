"""Small helpers used by core."""


def helper(n):
    """Add one."""
    return n + 1


def normalize(x):
    return helper(x) - 1


def scale(x, factor=2):
    return x * factor


def cached(fn):
    def wrapper(*args):
        return fn(*args)

    return wrapper


@cached
def compute(x):
    return helper(x)


def unused():
    return None
