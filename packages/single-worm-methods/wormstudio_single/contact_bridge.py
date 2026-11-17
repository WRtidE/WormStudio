"""Test the ordered 2-D posture of a short contact from its two clear boundaries."""
import numpy as np
from scipy import ndimage
from skimage.morphology import skeletonize

from .fusion import feasible_quality
from .geometry import topology_candidates
from .skeleton import skeleton_graph


def split_tip_states(mask, reference):
    """Try both interpretations of a short fork, testing each against the original mask."""
    skeleton = skeletonize(mask)
    if ndimage.label(skeleton, np.ones((3, 3)))[1] != 1:
        return []
    pixels, graph = skeleton_graph(skeleton)
    degrees = np.diff(graph.indptr)
    tips = np.flatnonzero(degrees == 1)
    if len(tips) != 3 or graph.nnz // 2 != len(pixels):
        return []
    radius = float(np.median(ndimage.distance_transform_edt(mask)[skeleton]))
    arms = []
    for tip in tips:
        path = [tip]
        node = tip
        previous = -1
        distance = 0.
        while node == tip or degrees[node] == 2:
            neighbors = graph.indices[graph.indptr[node]:graph.indptr[node + 1]]
            next_node = neighbors[neighbors != previous][0]
            distance += np.linalg.norm(pixels[next_node] - pixels[node])
            previous, node = node, next_node
            if degrees[node] != 2:
                break
            path.append(node)
        arms.append((tip, path, node, distance))
    pairs = [(a, b) for j, a in enumerate(arms) for b in arms[j+1:]
             if max(a[3], b[3]) <= 5 * radius
             and np.linalg.norm(pixels[a[2]] - pixels[b[2]]) <= radius
             and np.linalg.norm(pixels[a[0]] - pixels[b[0]]) <= 3 * radius]
    if len(pairs) != 1:
        return []
    states = []
    for tip, path, _, _ in pairs[0]:
        edited = skeleton.copy()
        edited[tuple(pixels[path].T)] = False
        proposals, _, _ = topology_candidates(mask, skeleton=edited)
        for proposal in proposals:
            line = proposal['points']
            quality = feasible_quality(line, mask, reference)
            if not quality['geometric_pass']:
                continue
            fork_end = np.linalg.norm(line[[0, -1]] - pixels[tip, ::-1], axis=1).argmin()
            observed = [True, True]
            observed[fork_end] = False
            for reverse in (False, True):
                states.append(dict(points=line[::-1] if reverse else line,
                    name='split_tip_' + proposal['name'], quality=quality,
                    endpoint_observed=observed[::-1] if reverse else observed))
    return states
