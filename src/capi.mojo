"""Byte-oriented XML tokenization and tree-step selection exposed through C ABI."""

from std.sys.info import simd_width_of as simdwidthof

comptime BPtr = UnsafePointer[UInt8, AnyOrigin[mut=True]]
comptime IPtr = UnsafePointer[Int, AnyOrigin[mut=True]]
comptime UPtr = UnsafePointer[UInt64, AnyOrigin[mut=True]]


def copy_ints(destination: IPtr, source: IPtr, count: Int):
    comptime W = simdwidthof[DType.float64]()
    var i = 0
    while i + W <= count:
        destination.store(i, source.unsafe_load[width=W](i))
        i += W
    while i < count:
        destination[i] = source[i]
        i += 1


def is_space(c: UInt8) -> Bool:
    return c == 32 or c == 9 or c == 10 or c == 13


def same_bytes(data: BPtr, a: Int, alen: Int, b: Int, blen: Int) -> Bool:
    if alen != blen:
        return False
    for i in range(alen):
        if data[a + i] != data[b + i]:
            return False
    return True


def starts(data: BPtr, n: Int, pos: Int, text: String) -> Bool:
    var raw = text.unsafe_ptr()
    var size = text.byte_length()
    if pos + size > n:
        return False
    for i in range(size):
        if data[pos + i] != raw[i]:
            return False
    return True


def emit(
    kinds: IPtr,
    aa: IPtr,
    bb: IPtr,
    cc: IPtr,
    dd: IPtr,
    count: Int,
    capacity: Int,
    kind: Int,
    a: Int,
    b: Int,
    c: Int = 0,
    d: Int = 0,
) -> Int:
    if count >= capacity:
        return -1
    kinds[count] = kind
    aa[count] = a
    bb[count] = b
    cc[count] = c
    dd[count] = d
    return count + 1


def tokenize(
    data: BPtr,
    n: Int,
    kinds: IPtr,
    aa: IPtr,
    bb: IPtr,
    cc: IPtr,
    dd: IPtr,
    stack_start: IPtr,
    stack_len: IPtr,
    capacity: Int,
    html_mode: Bool,
    error_info: IPtr,
) -> Int:
    var i = 0
    var count = 0
    var depth = 0
    var roots = 0
    if n >= 3 and data[0] == 239 and data[1] == 187 and data[2] == 191:
        i = 3

    while i < n:
        if data[i] != 60:
            var start_pos = i
            comptime W = simdwidthof[DType.float64]()
            while i + W <= n:
                var delimiters = data.unsafe_load[width=W](i).eq(UInt8(60)).cast[
                    DType.int64
                ]()
                if delimiters.reduce_add() != 0:
                    break
                i += W
            while i < n and data[i] != 60:
                i += 1
            if not html_mode and depth == 0:
                var non_space = False
                var j = start_pos
                while j + W <= i:
                    var chars = data.unsafe_load[width=W](j)
                    var spaces = (
                        chars.eq(UInt8(32)).cast[DType.int64]()
                        + chars.eq(UInt8(9)).cast[DType.int64]()
                        + chars.eq(UInt8(10)).cast[DType.int64]()
                        + chars.eq(UInt8(13)).cast[DType.int64]()
                    )
                    if spaces.reduce_add() != Int64(W):
                        non_space = True
                        break
                    j += W
                while not non_space and j < i:
                    if not is_space(data[j]):
                        non_space = True
                    j += 1
                if non_space:
                    error_info[0] = start_pos
                    error_info[1] = 1
                    return -1
            if depth > 0 or html_mode:
                count = emit(kinds, aa, bb, cc, dd, count, capacity, 3, start_pos, i)
                if count < 0:
                    error_info[0] = i
                    error_info[1] = 2
                    return -1
            continue

        if starts(data, n, i, "<!--"):
            var j = i + 4
            while j + 2 < n and not (
                data[j] == 45 and data[j + 1] == 45 and data[j + 2] == 62
            ):
                j += 1
            if j + 2 >= n:
                error_info[0] = i
                error_info[1] = 3
                return -1
            i = j + 3
            continue

        if starts(data, n, i, "<![CDATA["):
            var j = i + 9
            while j + 2 < n and not (
                data[j] == 93 and data[j + 1] == 93 and data[j + 2] == 62
            ):
                j += 1
            if j + 2 >= n:
                error_info[0] = i
                error_info[1] = 4
                return -1
            if depth > 0:
                count = emit(kinds, aa, bb, cc, dd, count, capacity, 3, i + 9, j, 1)
                if count < 0:
                    error_info[0] = i
                    error_info[1] = 2
                    return -1
            i = j + 3
            continue

        if starts(data, n, i, "<?"):
            var j = i + 2
            while j + 1 < n and not (data[j] == 63 and data[j + 1] == 62):
                j += 1
            if j + 1 >= n:
                error_info[0] = i
                error_info[1] = 5
                return -1
            i = j + 2
            continue

        if starts(data, n, i, "<!DOCTYPE") or starts(data, n, i, "<!doctype"):
            var j = i + 9
            var bracket_depth = 0
            var quote = UInt8(0)
            while j < n:
                var ch = data[j]
                if quote != 0:
                    if ch == quote:
                        quote = UInt8(0)
                elif ch == 34 or ch == 39:
                    quote = ch
                elif ch == 91:
                    bracket_depth += 1
                elif ch == 93 and bracket_depth > 0:
                    bracket_depth -= 1
                elif ch == 62 and bracket_depth == 0:
                    break
                j += 1
            if j >= n:
                error_info[0] = i
                error_info[1] = 6
                return -1
            i = j + 1
            continue

        if starts(data, n, i, "</"):
            var j = i + 2
            while j < n and is_space(data[j]):
                j += 1
            var name_start = j
            while j < n and not is_space(data[j]) and data[j] != 62:
                j += 1
            var name_len = j - name_start
            while j < n and is_space(data[j]):
                j += 1
            if name_len == 0 or j >= n or data[j] != 62:
                error_info[0] = i
                error_info[1] = 7
                return -1
            if not html_mode:
                if depth == 0 or not same_bytes(
                    data, name_start, name_len, Int(stack_start[depth - 1]), Int(stack_len[depth - 1])
                ):
                    error_info[0] = i
                    error_info[1] = 8
                    return -1
                depth -= 1
            count = emit(kinds, aa, bb, cc, dd, count, capacity, 4, name_start, j)
            if count < 0:
                error_info[0] = i
                error_info[1] = 2
                return -1
            i = j + 1
            continue

        if starts(data, n, i, "<!"):
            var j = i + 2
            while j < n and data[j] != 62:
                j += 1
            if j >= n:
                error_info[0] = i
                error_info[1] = 9
                return -1
            i = j + 1
            continue

        var j = i + 1
        while j < n and is_space(data[j]):
            j += 1
        var name_start = j
        while j < n and not is_space(data[j]) and data[j] != 62 and data[j] != 47:
            j += 1
        var name_len = j - name_start
        if name_len == 0:
            error_info[0] = i
            error_info[1] = 10
            return -1
        if not html_mode and depth == 0:
            roots += 1
            if roots > 1:
                error_info[0] = i
                error_info[1] = 11
                return -1
        var start_token = count
        count = emit(kinds, aa, bb, cc, dd, count, capacity, 1, name_start, j)
        if count < 0:
            error_info[0] = i
            error_info[1] = 2
            return -1
        var self_close = False

        while j < n:
            while j < n and is_space(data[j]):
                j += 1
            if j >= n:
                error_info[0] = i
                error_info[1] = 12
                return -1
            if data[j] == 62:
                j += 1
                break
            if data[j] == 47 and j + 1 < n and data[j + 1] == 62:
                self_close = True
                j += 2
                break

            var attr_start = j
            while j < n and not is_space(data[j]) and data[j] != 61 and data[j] != 62:
                j += 1
            var attr_end = j
            while j < n and is_space(data[j]):
                j += 1
            var value_start = j
            var value_end = j
            if j < n and data[j] == 61:
                j += 1
                while j < n and is_space(data[j]):
                    j += 1
                if j >= n:
                    error_info[0] = attr_start
                    error_info[1] = 13
                    return -1
                if data[j] == 34 or data[j] == 39:
                    var quote = data[j]
                    j += 1
                    value_start = j
                    while j < n and data[j] != quote:
                        j += 1
                    if j >= n:
                        error_info[0] = attr_start
                        error_info[1] = 14
                        return -1
                    value_end = j
                    j += 1
                elif html_mode:
                    value_start = j
                    while j < n and not is_space(data[j]) and data[j] != 62:
                        j += 1
                    value_end = j
                else:
                    error_info[0] = attr_start
                    error_info[1] = 15
                    return -1
            elif not html_mode:
                error_info[0] = attr_start
                error_info[1] = 16
                return -1
            count = emit(
                kinds, aa, bb, cc, dd, count, capacity, 2,
                attr_start, attr_end, value_start, value_end,
            )
            if count < 0:
                error_info[0] = attr_start
                error_info[1] = 2
                return -1

        cc[start_token] = 1 if self_close else 0
        dd[start_token] = count - start_token - 1
        if not html_mode and not self_close:
            stack_start[depth] = name_start
            stack_len[depth] = name_len
            depth += 1
        i = j

    if not html_mode and (depth != 0 or roots != 1):
        error_info[0] = n
        error_info[1] = 17
        return -1
    error_info[0] = -1
    error_info[1] = 0
    return count


def has_attr(
    node: Int,
    attr_name: UInt64,
    attr_value: UInt64,
    pred_mode: Int,
    attr_offsets: IPtr,
    attr_names: UPtr,
    attr_values: UPtr,
) -> Bool:
    if pred_mode == 0:
        return True
    for i in range(Int(attr_offsets[node]), Int(attr_offsets[node + 1])):
        if attr_names[i] == attr_name and (
            pred_mode == 1 or attr_values[i] == attr_value
        ):
            return True
    return False


def node_matches(
    node: Int,
    tag: UInt64,
    attr_name: UInt64,
    attr_value: UInt64,
    pred_mode: Int,
    tags: UPtr,
    attr_offsets: IPtr,
    attr_names: UPtr,
    attr_values: UPtr,
) -> Bool:
    if tag != 0 and tags[node] != tag:
        return False
    return has_attr(
        node, attr_name, attr_value, pred_mode,
        attr_offsets, attr_names, attr_values,
    )


def step_matches(
    node: Int,
    axis: Int,
    generation: Int,
    wanted_pos: Int,
    tag: UInt64,
    attr_name: UInt64,
    attr_value: UInt64,
    pred_mode: Int,
    parents: IPtr,
    tags: UPtr,
    attr_offsets: IPtr,
    attr_names: UPtr,
    attr_values: UPtr,
    marks: IPtr,
) -> Bool:
    var reachable = False
    if axis == 2:
        reachable = marks[node] == generation
    elif axis == 0:
        var parent = Int(parents[node])
        reachable = parent >= 0 and marks[parent] == generation
    else:
        var parent = Int(parents[node])
        while parent >= 0:
            if marks[parent] == generation:
                reachable = True
                break
            parent = Int(parents[parent])
    if not reachable or not node_matches(
        node, tag, attr_name, attr_value, pred_mode,
        tags, attr_offsets, attr_names, attr_values,
    ):
        return False
    if wanted_pos <= 0:
        return True
    var rank = 1
    var parent = parents[node]
    for previous in range(node):
        if parents[previous] == parent and node_matches(
            previous, tag, attr_name, attr_value, pred_mode,
            tags, attr_offsets, attr_names, attr_values,
        ):
            rank += 1
    return rank == wanted_pos


def parallel_scan(
    nnodes: Int,
    axis: Int,
    generation: Int,
    wanted_pos: Int,
    tag: UInt64,
    attr_name: UInt64,
    attr_value: UInt64,
    pred_mode: Int,
    parents_addr: Int,
    tags_addr: Int,
    attr_offsets_addr: Int,
    attr_names_addr: Int,
    attr_values_addr: Int,
    marks_addr: Int,
    result_addr: Int,
    task_counts_addr: Int,
):
    comptime chunk_size = 4096
    var task_count = (nnodes + chunk_size - 1) // chunk_size

    @__parameter
    def scan_task(task: Int):
        var parents = IPtr(unsafe_from_address=parents_addr)
        var tags = UPtr(unsafe_from_address=tags_addr)
        var attr_offsets = IPtr(unsafe_from_address=attr_offsets_addr)
        var attr_names = UPtr(unsafe_from_address=attr_names_addr)
        var attr_values = UPtr(unsafe_from_address=attr_values_addr)
        var marks = IPtr(unsafe_from_address=marks_addr)
        var result = IPtr(unsafe_from_address=result_addr)
        var task_counts = IPtr(unsafe_from_address=task_counts_addr)
        var begin = task * chunk_size
        var end = min(begin + chunk_size, nnodes)
        var count = 0
        var node = begin
        comptime W = simdwidthof[DType.float64]()
        if tag != 0:
            while node + W <= end:
                var tag_matches = tags.unsafe_load[width=W](node).eq(
                    tag
                ).cast[DType.int64]()
                if tag_matches.reduce_add() != 0:
                    for lane in range(W):
                        var candidate = node + lane
                        if tag_matches[lane] != 0 and step_matches(
                            candidate, axis, generation, wanted_pos, tag,
                            attr_name, attr_value, pred_mode, parents, tags,
                            attr_offsets, attr_names, attr_values, marks,
                        ):
                            result[begin + count] = candidate
                            count += 1
                node += W
        while node < end:
            if step_matches(
                node, axis, generation, wanted_pos, tag, attr_name, attr_value,
                pred_mode, parents, tags, attr_offsets, attr_names, attr_values,
                marks,
            ):
                result[begin + count] = node
                count += 1
            node += 1
        task_counts[task] = count

    for task in range(task_count):
        scan_task(task)


def select_nodes(
    parents: IPtr,
    tags: UPtr,
    nnodes: Int,
    attr_offsets: IPtr,
    attr_names: UPtr,
    attr_values: UPtr,
    nattrs: Int,
    starts_ptr: IPtr,
    nstarts: Int,
    axes: IPtr,
    step_tags: UPtr,
    pred_modes: IPtr,
    pred_names: UPtr,
    pred_values: UPtr,
    positions: IPtr,
    nsteps: Int,
    work_a: IPtr,
    work_b: IPtr,
    marks: IPtr,
    result: IPtr,
    task_counts: IPtr,
) -> Int:
    var current_count = nstarts
    copy_ints(work_a, starts_ptr, nstarts)
    var use_a = True

    for step in range(nsteps):
        var generation = step + 1
        if use_a:
            for i in range(current_count):
                marks[Int(work_a[i])] = generation
        else:
            for i in range(current_count):
                marks[Int(work_b[i])] = generation
        var next_count = 0
        var axis = Int(axes[step])
        var wanted_pos = Int(positions[step])
        if nnodes >= 262144:
            parallel_scan(
                nnodes, axis, generation, wanted_pos,
                step_tags[step], pred_names[step], pred_values[step],
                Int(pred_modes[step]), Int(parents), Int(tags), Int(attr_offsets),
                Int(attr_names), Int(attr_values), Int(marks), Int(result),
                Int(task_counts),
            )
            comptime chunk_size = 4096
            var task_count = (nnodes + chunk_size - 1) // chunk_size
            for task in range(task_count):
                var count = Int(task_counts[task])
                if use_a:
                    copy_ints(
                        work_b + next_count, result + task * chunk_size, count,
                    )
                else:
                    copy_ints(
                        work_a + next_count, result + task * chunk_size, count,
                    )
                next_count += count
        else:
            var node = 0
            comptime W = simdwidthof[DType.float64]()
            if step_tags[step] != 0:
                while node + W <= nnodes:
                    var tag_matches = tags.unsafe_load[width=W](node).eq(
                        step_tags[step]
                    ).cast[DType.int64]()
                    if tag_matches.reduce_add() != 0:
                        for lane in range(W):
                            var candidate = node + lane
                            if tag_matches[lane] != 0 and step_matches(
                                candidate, axis, generation, wanted_pos,
                                step_tags[step], pred_names[step],
                                pred_values[step], Int(pred_modes[step]),
                                parents, tags, attr_offsets, attr_names,
                                attr_values, marks,
                            ):
                                if use_a:
                                    work_b[next_count] = candidate
                                else:
                                    work_a[next_count] = candidate
                                next_count += 1
                    node += W
            while node < nnodes:
                if step_matches(
                    node, axis, generation, wanted_pos,
                    step_tags[step], pred_names[step], pred_values[step],
                    Int(pred_modes[step]), parents, tags, attr_offsets,
                    attr_names, attr_values, marks,
                ):
                    if use_a:
                        work_b[next_count] = node
                    else:
                        work_a[next_count] = node
                    next_count += 1
                node += 1
        if use_a:
            for i in range(current_count):
                marks[Int(work_a[i])] = 0
        else:
            for i in range(current_count):
                marks[Int(work_b[i])] = 0
        current_count = next_count
        use_a = not use_a

    if nsteps == 0:
        copy_ints(result, work_a, current_count)
    elif use_a:
        copy_ints(result, work_a, current_count)
    else:
        copy_ints(result, work_b, current_count)
    return current_count


def select_nodes_from_addresses(
    parents_addr: Int,
    tags_addr: Int,
    nnodes: Int,
    attr_owner_addr: Int,
    attr_names_addr: Int,
    attr_values_addr: Int,
    nattrs: Int,
    starts_addr: Int,
    nstarts: Int,
    axes_addr: Int,
    step_tags_addr: Int,
    pred_modes_addr: Int,
    pred_names_addr: Int,
    pred_values_addr: Int,
    positions_addr: Int,
    nsteps: Int,
    work_a_addr: Int,
    work_b_addr: Int,
    marks_addr: Int,
    result_addr: Int,
    task_counts_addr: Int,
) -> Int:
    return select_nodes(
        IPtr(unsafe_from_address=parents_addr),
        UPtr(unsafe_from_address=tags_addr), nnodes,
        IPtr(unsafe_from_address=attr_owner_addr),
        UPtr(unsafe_from_address=attr_names_addr),
        UPtr(unsafe_from_address=attr_values_addr), nattrs,
        IPtr(unsafe_from_address=starts_addr), nstarts,
        IPtr(unsafe_from_address=axes_addr),
        UPtr(unsafe_from_address=step_tags_addr),
        IPtr(unsafe_from_address=pred_modes_addr),
        UPtr(unsafe_from_address=pred_names_addr),
        UPtr(unsafe_from_address=pred_values_addr),
        IPtr(unsafe_from_address=positions_addr), nsteps,
        IPtr(unsafe_from_address=work_a_addr),
        IPtr(unsafe_from_address=work_b_addr),
        IPtr(unsafe_from_address=marks_addr),
        IPtr(unsafe_from_address=result_addr),
        IPtr(unsafe_from_address=task_counts_addr),
    )


@export("mlx_tokenize")
def mlx_tokenize(
    data_addr: Int,
    n: Int,
    kinds_addr: Int,
    a_addr: Int,
    b_addr: Int,
    c_addr: Int,
    d_addr: Int,
    stack_start_addr: Int,
    stack_len_addr: Int,
    capacity: Int,
    html_mode: Int,
    error_addr: Int,
) abi("C") -> Int:
    return tokenize(
        BPtr(unsafe_from_address=data_addr), n,
        IPtr(unsafe_from_address=kinds_addr),
        IPtr(unsafe_from_address=a_addr),
        IPtr(unsafe_from_address=b_addr),
        IPtr(unsafe_from_address=c_addr),
        IPtr(unsafe_from_address=d_addr),
        IPtr(unsafe_from_address=stack_start_addr),
        IPtr(unsafe_from_address=stack_len_addr),
        capacity, html_mode != 0,
        IPtr(unsafe_from_address=error_addr),
    )


@export("mlx_select")
def mlx_select(
    parents_addr: Int,
    tags_addr: Int,
    nnodes: Int,
    attr_owner_addr: Int,
    attr_names_addr: Int,
    attr_values_addr: Int,
    nattrs: Int,
    starts_addr: Int,
    nstarts: Int,
    axes_addr: Int,
    step_tags_addr: Int,
    pred_modes_addr: Int,
    pred_names_addr: Int,
    pred_values_addr: Int,
    positions_addr: Int,
    nsteps: Int,
    work_a_addr: Int,
    work_b_addr: Int,
    marks_addr: Int,
    result_addr: Int,
    task_counts_addr: Int,
) abi("C") -> Int:
    return select_nodes_from_addresses(
        parents_addr, tags_addr, nnodes,
        attr_owner_addr, attr_names_addr, attr_values_addr, nattrs,
        starts_addr, nstarts, axes_addr, step_tags_addr, pred_modes_addr,
        pred_names_addr, pred_values_addr, positions_addr, nsteps,
        work_a_addr, work_b_addr, marks_addr, result_addr, task_counts_addr,
    )
