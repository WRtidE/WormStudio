"""按通道记录从下向上穿过检测线的观测轨迹。"""


def count_crossings(
    active_worms: dict,
    counts_dict: dict,
    channel_bounds: list,
    tripwire_y: int,
    cross_cooldown: dict,
    cross_debounce: int = 20,
    *,
    events: list | None = None,
    armed: set | None = None,
    arm_distance: int = 0,
) -> tuple:
    """按通道边界归属越线事件。

    判定逻辑：自下而上穿过 tripwire（图像坐标 Y 减小）。
    归属策略：用两次观测插值越线位置，只计入实际通道内的穿越。

    Args:
        active_worms: {worm_id: deque([(cx,cy), ...])}
        counts_dict: {channel_label: count}
        channel_bounds: [(left, right), ...]
        tripwire_y: 检测线 Y 坐标
        cross_cooldown: {worm_id: remaining_cooldown_frames}
        cross_debounce: 越线后冷却帧数
        events: 可选列表，逐次记录本帧实际计入的越线
        armed: 已在检测线下方观察到的轨迹；计入后需重新离开检测线
        arm_distance: 建立下方观测所需的最小距离（像素）

    Returns:
        (counts_dict, cross_cooldown, crossing_channels)
        crossing_channels: 本帧发生穿越的通道 ID 集合
    """
    crossing_channels = set()

    for wid, history in active_worms.items():
        if not history:
            continue
        p2 = history[-1]
        if armed is not None and p2[1] >= tripwire_y + arm_distance:
            armed.add(wid)
        if len(history) < 2 or cross_cooldown.get(wid, 0) > 0:
            continue
        if armed is not None and wid not in armed:
            continue

        p1 = history[-2]
        if p1[1] >= tripwire_y and p2[1] < tripwire_y:
            fraction = (p1[1] - tripwire_y) / (p1[1] - p2[1])
            cross_x = p1[0] + fraction * (p2[0] - p1[0])
            best_idx = next((i for i, (left, right) in enumerate(channel_bounds)
                             if left <= cross_x <= right), None)
            if best_idx is None:
                continue
            if armed is not None:
                armed.discard(wid)
            current_total_channels = len(channel_bounds)
            found_channel = current_total_channels - best_idx

            counts_dict[found_channel] += 1
            cross_cooldown[wid] = cross_debounce
            crossing_channels.add(found_channel)
            if events is not None:
                events.append(dict(channel=int(found_channel), track=int(wid),
                                   prev_x=int(p1[0]), prev_y=int(p1[1]), x=int(p2[0]), y=int(p2[1]),
                                   bounds=list(map(int, channel_bounds[best_idx])), line_y=int(tripwire_y)))

    return counts_dict, cross_cooldown, crossing_channels
