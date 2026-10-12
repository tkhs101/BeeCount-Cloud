def f(x, flag):
    total = 0
    if flag:
        total += 1
    for i in range(3):
        total += i
    while x:
        x -= 1
        total += 1
    try:
        total += 2
    except Exception:
        pass
    return total
