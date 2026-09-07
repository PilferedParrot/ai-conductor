def is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

def is_positive_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
