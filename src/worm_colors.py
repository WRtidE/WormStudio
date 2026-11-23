"""Consistent BGR colors for positive object IDs in every pipeline stage."""
import colorsys


def worm_color(object_id):
    known = {1: (40, 210, 50), 2: (220, 40, 210),
             3: (220, 200, 20), 4: (40, 70, 240)}
    if object_id in known:
        return known[object_id]
    rgb = colorsys.hsv_to_rgb((object_id * 0.61803398875) % 1, .78, .95)
    return tuple(round(255 * value) for value in rgb[::-1])
