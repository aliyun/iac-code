/* Deterministic, terminating DiagramGraph v1 layout. This file stays import-free so the
 * sandboxed frame can start it from a Blob and terminate it on timeout or cancellation. */
const LIMITS = Object.freeze({ nodes: 80, containers: 24, edges: 160, labelChars: 120 });
const GEOMETRY = Object.freeze({
  nodeWidth: 190,
  nodeMaxWidth: 340,
  nodeHeight: 88,
  columnGap: 48,
  rowGap: 32,
  paddingX: 20,
  paddingBottom: 24,
  titleHeight: 54,
  rootPadding: 16,
});

function assertGraph(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== 1) {
    throw new Error("Unsupported architecture graph");
  }
  const nodes = Array.isArray(value.nodes) ? value.nodes : [];
  const containers = Array.isArray(value.containers) ? value.containers : [];
  const edges = Array.isArray(value.edges) ? value.edges : [];
  if (nodes.length > LIMITS.nodes || containers.length > LIMITS.containers || edges.length > LIMITS.edges) {
    throw new Error("Architecture graph is too large");
  }
  const items = [];
  const ids = new Set();
  for (const [kind, list] of [["node", nodes], ["container", containers]]) {
    for (const raw of list) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Invalid architecture item");
      const id = String(raw.id || "").trim();
      const label = String(raw.label || id).trim().slice(0, LIMITS.labelChars);
      const parentId = raw.parentId == null ? null : String(raw.parentId).trim();
      if (!id || ids.has(id) || !label) throw new Error("Invalid or duplicate architecture id");
      ids.add(id);
      items.push({
        id,
        label,
        parentId: parentId || null,
        kind,
        resourceType: String(raw.resourceType || "").slice(0, LIMITS.labelChars),
        product: String(raw.product || "").slice(0, LIMITS.labelChars),
        role: String(raw.role || "").slice(0, LIMITS.labelChars),
      });
    }
  }

  // Planning graphs emitted before nested containers were supported kept a VSwitch as both a
  // node inside the VPC and a root-level `group_vswitch` container. Repair that precise legacy
  // shape at render time so saved sessions gain the corrected hierarchy without regeneration.
  const legacyAnchors = new Map();
  const initialContainers = new Map(
    items.filter((item) => item.kind === "container").map((item) => [item.id, item]),
  );
  for (const item of items) {
    if (item.kind !== "node" || !item.parentId) continue;
    const container = initialContainers.get(`group_${item.id}`);
    if (!container || container.parentId || container.id === item.parentId) continue;
    container.parentId = item.parentId;
    container.label = item.label;
    legacyAnchors.set(item.id, container.id);
  }
  if (legacyAnchors.size) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (legacyAnchors.has(items[index].id)) items.splice(index, 1);
    }
  }

  ids.clear();
  for (const item of items) ids.add(item.id);
  const containersById = new Map(items.filter((item) => item.kind === "container").map((item) => [item.id, item]));
  for (const item of items) {
    if (item.parentId && !containersById.has(item.parentId)) throw new Error("Unknown architecture parent");
  }
  for (const container of containersById.values()) {
    const seen = new Set([container.id]);
    let current = container;
    while (current.parentId) {
      if (seen.has(current.parentId)) throw new Error("Cyclic architecture containment");
      seen.add(current.parentId);
      current = containersById.get(current.parentId);
    }
  }
  const itemsById = new Map(items.map((item) => [item.id, item]));
  function isDescendant(itemId, containerId) {
    let current = itemsById.get(itemId);
    const seen = new Set();
    while (current?.parentId && !seen.has(current.parentId)) {
      if (current.parentId === containerId) return true;
      seen.add(current.parentId);
      current = containersById.get(current.parentId);
    }
    return false;
  }
  const normalizedEdges = [];
  const seenEdges = new Set();
  for (let index = 0; index < edges.length; index += 1) {
    const raw = edges[index];
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Invalid architecture edge");
    const rawFrom = String(raw.from || "").trim();
    const rawTo = String(raw.to || "").trim();
    const from = legacyAnchors.get(rawFrom) || rawFrom;
    const to = legacyAnchors.get(rawTo) || rawTo;
    if (!ids.has(from) || !ids.has(to)) throw new Error("Invalid architecture edge endpoint");
    const rewritten = from !== rawFrom || to !== rawTo;
    if (from === to) {
      if (rewritten) continue;
      throw new Error("Invalid architecture edge endpoint");
    }
    if (rewritten && (isDescendant(to, from) || isDescendant(from, to))) continue;
    const label = String(raw.label || "").trim().slice(0, LIMITS.labelChars);
    const style = ["solid_arrow", "dotted_arrow", "dotted_open"].includes(raw.style)
      ? raw.style
      : "solid_arrow";
    const key = `${from}\u0000${to}\u0000${label}\u0000${style}`;
    if (seenEdges.has(key)) continue;
    seenEdges.add(key);
    normalizedEdges.push({ id: String(raw.id || `edge_${index + 1}`), from, to, label, style });
  }
  if (!items.length) throw new Error("Architecture graph is empty");
  items.sort((left, right) => left.id.localeCompare(right.id));
  normalizedEdges.sort(
    (left, right) =>
      left.from.localeCompare(right.from) ||
      left.to.localeCompare(right.to) ||
      left.label.localeCompare(right.label) ||
      left.style.localeCompare(right.style) ||
      left.id.localeCompare(right.id),
  );
  return { items, edges: normalizedEdges };
}

function stronglyConnected(ids, adjacency) {
  let cursor = 0;
  const indices = new Map();
  const lows = new Map();
  const stack = [];
  const active = new Set();
  const components = [];
  function visit(id) {
    indices.set(id, cursor);
    lows.set(id, cursor);
    cursor += 1;
    stack.push(id);
    active.add(id);
    for (const target of [...(adjacency.get(id) || [])].sort()) {
      if (!indices.has(target)) {
        visit(target);
        lows.set(id, Math.min(lows.get(id), lows.get(target)));
      } else if (active.has(target)) {
        lows.set(id, Math.min(lows.get(id), indices.get(target)));
      }
    }
    if (lows.get(id) !== indices.get(id)) return;
    const component = [];
    while (stack.length) {
      const member = stack.pop();
      active.delete(member);
      component.push(member);
      if (member === id) break;
    }
    components.push(component.sort());
  }
  for (const id of [...ids].sort()) if (!indices.has(id)) visit(id);
  return components;
}

function rankedChildren(childIds, projectedEdges) {
  const adjacency = new Map(childIds.map((id) => [id, new Set()]));
  for (const edge of projectedEdges) adjacency.get(edge.from)?.add(edge.to);
  const components = stronglyConnected(childIds, adjacency);
  const componentById = new Map();
  components.forEach((component, index) => component.forEach((id) => componentById.set(id, index)));
  const dag = new Map(components.map((_component, index) => [index, new Set()]));
  const indegree = new Map(components.map((_component, index) => [index, 0]));
  for (const edge of projectedEdges) {
    const from = componentById.get(edge.from);
    const to = componentById.get(edge.to);
    if (from === to || dag.get(from).has(to)) continue;
    dag.get(from).add(to);
    indegree.set(to, indegree.get(to) + 1);
  }
  const rank = new Map(components.map((_component, index) => [index, 0]));
  const queue = [...indegree.entries()].filter(([, value]) => value === 0).map(([id]) => id).sort((a, b) => a - b);
  while (queue.length) {
    const current = queue.shift();
    for (const target of [...dag.get(current)].sort((a, b) => a - b)) {
      rank.set(target, Math.max(rank.get(target), rank.get(current) + 1));
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    }
    queue.sort((a, b) => a - b);
  }
  return new Map(childIds.map((id) => [id, rank.get(componentById.get(id)) || 0]));
}

function estimatedTextWidth(value) {
  return [...String(value || "")].reduce((total, character) => {
    if (/\s/.test(character)) return total + 4;
    return total + (character.charCodeAt(0) > 127 ? 14 : 8);
  }, 0);
}

function nodeDimensions(label) {
  const sourceLines = String(label || "").split("\n");
  const widestLine = Math.max(...sourceLines.map(estimatedTextWidth), 0);
  const width = Math.max(GEOMETRY.nodeWidth, Math.min(GEOMETRY.nodeMaxWidth, Math.ceil(widestLine + 58)));
  const contentWidth = Math.max(80, width - 58);
  const wrappedLines = sourceLines.reduce(
    (total, line) => total + Math.max(1, Math.ceil(estimatedTextWidth(line) / contentWidth)),
    0,
  );
  const listLines = sourceLines.filter((line) => /^\s*[+*-]\s+/.test(line)).length;
  const markdownClearance = listLines * 8 + (sourceLines.length > 1 ? 12 : 0);
  return {
    width,
    height: Math.max(GEOMETRY.nodeHeight, Math.min(240, 52 + wrappedLines * 18 + markdownClearance)),
  };
}

function packedGrid(rawBoxes, gap, targetAspect) {
  const boxes = [...rawBoxes].sort((left, right) => left.item.id.localeCompare(right.item.id));
  if (!boxes.length) return { placements: [], width: 0, height: 0 };
  let best = null;
  for (let columnCount = 1; columnCount <= boxes.length; columnCount += 1) {
    const rowCount = Math.ceil(boxes.length / columnCount);
    const columnWidths = Array(columnCount).fill(0);
    const rowHeights = Array(rowCount).fill(0);
    boxes.forEach((box, index) => {
      const column = index % columnCount;
      const row = Math.floor(index / columnCount);
      columnWidths[column] = Math.max(columnWidths[column], box.width);
      rowHeights[row] = Math.max(rowHeights[row], box.height);
    });
    const width = columnWidths.reduce((total, value) => total + value, 0) + gap * (columnCount - 1);
    const height = rowHeights.reduce((total, value) => total + value, 0) + gap * (rowCount - 1);
    const unusedSlots = columnCount * rowCount - boxes.length;
    const score = Math.abs(Math.log(Math.max(0.01, width / Math.max(1, height)) / targetAspect))
      + unusedSlots * 0.035;
    if (
      best &&
      (score > best.score + 1e-9 ||
        (Math.abs(score - best.score) <= 1e-9 && width * height >= best.width * best.height))
    ) continue;
    const columnX = [];
    const rowY = [];
    let cursor = 0;
    for (const value of columnWidths) {
      columnX.push(cursor);
      cursor += value + gap;
    }
    cursor = 0;
    for (const value of rowHeights) {
      rowY.push(cursor);
      cursor += value + gap;
    }
    const placements = boxes.map((box, index) => {
      const column = index % columnCount;
      const row = Math.floor(index / columnCount);
      return {
        ...box,
        x: columnX[column] + Math.round((columnWidths[column] - box.width) / 2),
        y: rowY[row] + Math.round((rowHeights[row] - box.height) / 2),
      };
    });
    best = { score, placements, width, height };
  }
  return best;
}

function compactRoute(points) {
  const compact = [];
  for (const point of points) {
    const previous = compact[compact.length - 1];
    if (previous && point[0] === previous[0] && point[1] === previous[1]) continue;
    compact.push(point);
  }
  for (let index = compact.length - 2; index > 0; index -= 1) {
    const before = compact[index - 1];
    const current = compact[index];
    const after = compact[index + 1];
    if ((before[0] === current[0] && current[0] === after[0]) || (before[1] === current[1] && current[1] === after[1])) {
      compact.splice(index, 1);
    }
  }
  return compact;
}

function rectPort(rect, side) {
  if (side === "left") return [rect.x, rect.y + rect.height / 2];
  if (side === "right") return [rect.x + rect.width, rect.y + rect.height / 2];
  if (side === "top") return [rect.x + rect.width / 2, rect.y];
  return [rect.x + rect.width / 2, rect.y + rect.height];
}

function portExit(point, side, clearance = 10) {
  if (side === "left") return [point[0] - clearance, point[1]];
  if (side === "right") return [point[0] + clearance, point[1]];
  if (side === "top") return [point[0], point[1] - clearance];
  return [point[0], point[1] + clearance];
}

function segmentIntersectsRect(start, end, rect, margin = 4) {
  const left = rect.x + margin;
  const right = rect.x + rect.width - margin;
  const top = rect.y + margin;
  const bottom = rect.y + rect.height - margin;
  if (start[0] === end[0]) {
    return left < start[0] && start[0] < right && Math.max(start[1], end[1]) > top && Math.min(start[1], end[1]) < bottom;
  }
  if (start[1] === end[1]) {
    return top < start[1] && start[1] < bottom && Math.max(start[0], end[0]) > left && Math.min(start[0], end[0]) < right;
  }
  return true;
}

function routeObstacleCount(points, obstacles) {
  let count = 0;
  for (const obstacle of obstacles) {
    if (points.slice(1).some((point, index) => segmentIntersectsRect(points[index], point, obstacle))) count += 1;
  }
  return count;
}

function routeLength(points) {
  return points.slice(1).reduce(
    (total, point, index) => total + Math.abs(point[0] - points[index][0]) + Math.abs(point[1] - points[index][1]),
    0,
  );
}

function obstacleAvoidingRoute(from, to, obstacles, root, preferred) {
  const candidates = [{ ...preferred, points: compactRoute(preferred.points), preference: 0 }];
  const add = (fromPort, toPort, points, preference) => {
    candidates.push({ fromPort, toPort, points: compactRoute(points), preference });
  };
  const fromCenter = [from.x + from.width / 2, from.y + from.height / 2];
  const toCenter = [to.x + to.width / 2, to.y + to.height / 2];
  const horizontalForward = toCenter[0] >= fromCenter[0];
  const horizontalFrom = horizontalForward ? "right" : "left";
  const horizontalTo = horizontalForward ? "left" : "right";
  const horizontalStart = rectPort(from, horizontalFrom);
  const horizontalEnd = rectPort(to, horizontalTo);
  const horizontalExit = portExit(horizontalStart, horizontalFrom);
  const horizontalEntry = portExit(horizontalEnd, horizontalTo);
  const horizontalMiddle = Math.round((horizontalExit[0] + horizontalEntry[0]) / 2);
  add(
    horizontalFrom,
    horizontalTo,
    [horizontalStart, horizontalExit, [horizontalMiddle, horizontalExit[1]], [horizontalMiddle, horizontalEntry[1]], horizontalEntry, horizontalEnd],
    4,
  );

  const verticalLanes = new Set([8, root.height - 8]);
  for (const obstacle of obstacles) {
    verticalLanes.add(Math.max(8, obstacle.y - 10));
    verticalLanes.add(Math.min(root.height - 8, obstacle.y + obstacle.height + 10));
  }
  for (const lane of [...verticalLanes].sort((left, right) => left - right)) {
    add(
      horizontalFrom,
      horizontalTo,
      [horizontalStart, horizontalExit, [horizontalExit[0], lane], [horizontalEntry[0], lane], horizontalEntry, horizontalEnd],
      12,
    );
  }

  const verticalForward = toCenter[1] >= fromCenter[1];
  const verticalFrom = verticalForward ? "bottom" : "top";
  const verticalTo = verticalForward ? "top" : "bottom";
  const verticalStart = rectPort(from, verticalFrom);
  const verticalEnd = rectPort(to, verticalTo);
  const verticalExit = portExit(verticalStart, verticalFrom);
  const verticalEntry = portExit(verticalEnd, verticalTo);
  const verticalMiddle = Math.round((verticalExit[1] + verticalEntry[1]) / 2);
  add(
    verticalFrom,
    verticalTo,
    [verticalStart, verticalExit, [verticalExit[0], verticalMiddle], [verticalEntry[0], verticalMiddle], verticalEntry, verticalEnd],
    5,
  );

  const horizontalLanes = new Set([8, root.width - 8]);
  for (const obstacle of obstacles) {
    horizontalLanes.add(Math.max(8, obstacle.x - 10));
    horizontalLanes.add(Math.min(root.width - 8, obstacle.x + obstacle.width + 10));
  }
  for (const lane of [...horizontalLanes].sort((left, right) => left - right)) {
    add(
      verticalFrom,
      verticalTo,
      [verticalStart, verticalExit, [lane, verticalExit[1]], [lane, verticalEntry[1]], verticalEntry, verticalEnd],
      13,
    );
  }

  for (const side of ["left", "right"]) {
    const start = rectPort(from, side);
    const end = rectPort(to, side);
    const lane = side === "left"
      ? Math.max(6, Math.min(from.x, to.x, ...obstacles.map((item) => item.x)) - 10)
      : Math.min(
          root.width - 6,
          Math.max(from.x + from.width, to.x + to.width, ...obstacles.map((item) => item.x + item.width)) + 10,
        );
    add(side, side, [start, [lane, start[1]], [lane, end[1]], end], 18);
  }
  for (const side of ["top", "bottom"]) {
    const start = rectPort(from, side);
    const end = rectPort(to, side);
    const lane = side === "top"
      ? Math.max(6, Math.min(from.y, to.y, ...obstacles.map((item) => item.y)) - 10)
      : Math.min(
          root.height - 6,
          Math.max(from.y + from.height, to.y + to.height, ...obstacles.map((item) => item.y + item.height)) + 10,
        );
    add(side, side, [start, [start[0], lane], [end[0], lane], end], 19);
  }

  candidates.sort((left, right) => {
    const leftHits = routeObstacleCount(left.points, obstacles);
    const rightHits = routeObstacleCount(right.points, obstacles);
    if (leftHits !== rightHits) return leftHits - rightHits;
    const leftCost = routeLength(left.points) + Math.max(0, left.points.length - 2) * 18 + left.preference;
    const rightCost = routeLength(right.points) + Math.max(0, right.points.length - 2) * 18 + right.preference;
    if (leftCost !== rightCost) return leftCost - rightCost;
    return JSON.stringify(left.points).localeCompare(JSON.stringify(right.points));
  });
  return candidates[0];
}

function labelPlacementForPath(label, points, obstacles, root) {
  if (!label) return null;
  const width = Math.min(160, Math.max(40, estimatedTextWidth(label) + 12));
  const height = 22;
  const segments = points.slice(1).map((point, index) => {
    const start = points[index];
    return {
      start,
      end: point,
      horizontal: start[1] === point[1],
      length: Math.abs(point[0] - start[0]) + Math.abs(point[1] - start[1]),
    };
  });
  segments.sort((left, right) => Number(right.horizontal) - Number(left.horizontal) || right.length - left.length);
  for (const segment of segments) {
    if (segment.length < width + 12) continue;
    const x = segment.horizontal
      ? Math.round((segment.start[0] + segment.end[0] - width) / 2)
      : Math.round(segment.start[0] - width / 2);
    const y = segment.horizontal
      ? Math.round(segment.start[1] - height / 2)
      : Math.round((segment.start[1] + segment.end[1] - height) / 2);
    const placement = { x, y, width, height };
    if (x < 4 || y < 4 || x + width > root.width - 4 || y + height > root.height - 4) continue;
    if (obstacles.some((obstacle) =>
      placement.x < obstacle.x + obstacle.width &&
      obstacle.x < placement.x + placement.width &&
      placement.y < obstacle.y + obstacle.height &&
      obstacle.y < placement.y + placement.height
    )) continue;
    return placement;
  }
  return null;
}

function buildLayout(graph) {
  const { items, edges } = assertGraph(graph);
  const byId = new Map(items.map((item) => [item.id, item]));
  const children = new Map([[null, []]]);
  for (const item of items) {
    if (!children.has(item.parentId)) children.set(item.parentId, []);
    children.get(item.parentId).push(item);
  }
  for (const list of children.values()) list.sort((a, b) => a.id.localeCompare(b.id));

  function branchAt(endpoint, parentId) {
    let current = byId.get(endpoint);
    if (!current || current.id === parentId) return null;
    while (current.parentId !== parentId) {
      if (!current.parentId) return parentId === null ? current.id : null;
      current = byId.get(current.parentId);
      if (!current) return null;
    }
    return current.id;
  }

  function layoutScope(parentId) {
    const direct = children.get(parentId) || [];
    const boxes = new Map();
    for (const item of direct) {
      if (item.kind === "container") {
        const subtree = layoutScope(item.id);
        boxes.set(item.id, { item, subtree, width: subtree.width, height: subtree.height });
      } else {
        const dimensions = nodeDimensions(item.label);
        boxes.set(item.id, {
          item,
          subtree: null,
          ...dimensions,
        });
      }
    }
    const primaryProjected = [];
    const fallbackProjected = [];
    for (const edge of edges) {
      const from = branchAt(edge.from, parentId);
      const to = branchAt(edge.to, parentId);
      if (!from || !to || from === to || !boxes.has(from) || !boxes.has(to)) continue;
      const projection = { from, to };
      fallbackProjected.push(projection);
      if (edge.style === "solid_arrow") primaryProjected.push(projection);
    }
    // Traffic and explicit dependency edges remain the primary ordering signal. When a scope
    // contains only inferred or management relationships, those edges still provide a better
    // reading order than an arbitrary vertical stack.
    const projected = primaryProjected.length ? primaryProjected : fallbackProjected;
    const ranks = rankedChildren([...boxes.keys()], projected);
    const rankGroups = new Map();
    for (const id of [...boxes.keys()].sort()) {
      const rank = ranks.get(id) || 0;
      if (!rankGroups.has(rank)) rankGroups.set(rank, []);
      rankGroups.get(rank).push(boxes.get(id));
    }
    const startX = parentId === null ? GEOMETRY.rootPadding : GEOMETRY.paddingX;
    const startY = parentId === null ? GEOMETRY.rootPadding : GEOMETRY.titleHeight;
    const placements = [];
    const orderedRanks = [...rankGroups.keys()].sort((a, b) => a - b);

    if (parentId === null) {
      const bands = orderedRanks.map((rank) =>
        packedGrid(rankGroups.get(rank), GEOMETRY.rowGap, orderedRanks.length > 1 ? 0.75 : 1.6),
      );
      const contentHeight = Math.max(...bands.map((band) => band.height), 0);
      let x = startX;
      for (const band of bands) {
        const y = startY + Math.round((contentHeight - band.height) / 2);
        for (const placement of band.placements) {
          placements.push({ ...placement, x: x + placement.x, y: y + placement.y });
        }
        x += band.width + GEOMETRY.columnGap;
      }
      const usedWidth = placements.length ? x - GEOMETRY.columnGap : startX + GEOMETRY.nodeWidth;
      return {
        placements,
        width: Math.max(360, usedWidth + GEOMETRY.rootPadding),
        height: Math.max(180, startY + contentHeight + GEOMETRY.rootPadding),
      };
    }

    // Inside a network boundary, advance dependency ranks from top to bottom. This keeps nested
    // VPC/VSwitch structures compact while peers in the same rank still share a horizontal row.
    const bands = orderedRanks.map((rank) => packedGrid(rankGroups.get(rank), GEOMETRY.rowGap, 1.6));
    const contentWidth = Math.max(...bands.map((band) => band.width), 0);
    let y = startY;
    for (const band of bands) {
      const x = startX + Math.round((contentWidth - band.width) / 2);
      for (const placement of band.placements) {
        placements.push({ ...placement, x: x + placement.x, y: y + placement.y });
      }
      y += band.height + GEOMETRY.rowGap;
    }
    const usedHeight = placements.length ? y - GEOMETRY.rowGap : startY + GEOMETRY.nodeHeight;
    return {
      placements,
      width: Math.max(240, startX + contentWidth + GEOMETRY.paddingX),
      height: Math.max(160, usedHeight + GEOMETRY.paddingBottom),
    };
  }

  const rects = new Map();
  function flatten(scope, offsetX, offsetY) {
    for (const placement of scope.placements) {
      const x = offsetX + placement.x;
      const y = offsetY + placement.y;
      rects.set(placement.item.id, { x, y, width: placement.width, height: placement.height });
      if (placement.subtree) flatten(placement.subtree, x, y);
    }
  }
  const root = layoutScope(null);
  flatten(root, 0, 0);

  function scopes(itemId) {
    const result = [];
    let current = byId.get(itemId);
    while (current) {
      result.push(current.parentId);
      current = current.parentId ? byId.get(current.parentId) : null;
    }
    return result;
  }

  function route(edge) {
    const from = rects.get(edge.from);
    const to = rects.get(edge.to);
    if (!from || !to) throw new Error("Missing routed endpoint");
    const fromCenter = [from.x + from.width / 2, from.y + from.height / 2];
    const toCenter = [to.x + to.width / 2, to.y + to.height / 2];
    let points;
    let fromPort;
    let toPort;
    let labelCorridor = null;

    const targetScopes = new Set(scopes(edge.to));
    const commonScope = scopes(edge.from).find((scope) => targetScopes.has(scope));
    const fromBranch = commonScope === undefined ? null : branchAt(edge.from, commonScope);
    const toBranch = commonScope === undefined ? null : branchAt(edge.to, commonScope);
    const fromBranchRect = rects.get(fromBranch);
    const toBranchRect = rects.get(toBranch);
    const fromAbove = Boolean(
      fromBranchRect && toBranchRect && fromBranchRect.y + fromBranchRect.height <= toBranchRect.y,
    );
    const toAbove = Boolean(
      fromBranchRect && toBranchRect && toBranchRect.y + toBranchRect.height <= fromBranchRect.y,
    );

    if (fromAbove && toBranch !== edge.to) {
      const gapStart = fromBranchRect.y + fromBranchRect.height;
      const gapEnd = toBranchRect.y;
      const clearanceY = Math.round((gapStart + gapEnd) / 2);
      const sideX = toBranchRect.x + toBranchRect.width + 10;
      points = [
        [fromCenter[0], from.y + from.height],
        [fromCenter[0], clearanceY],
        [sideX, clearanceY],
        [sideX, toCenter[1]],
        [to.x + to.width, toCenter[1]],
      ];
      fromPort = "bottom";
      toPort = "right";
      labelCorridor = { start: fromCenter[0], end: sideX, y: clearanceY, gapStart, gapEnd };
    } else if (toAbove && fromBranch !== edge.from) {
      const gapStart = toBranchRect.y + toBranchRect.height;
      const gapEnd = fromBranchRect.y;
      const clearanceY = Math.round((gapStart + gapEnd) / 2);
      const sideX = fromBranchRect.x + fromBranchRect.width + 10;
      points = [
        [from.x + from.width, fromCenter[1]],
        [sideX, fromCenter[1]],
        [sideX, clearanceY],
        [toCenter[0], clearanceY],
        [toCenter[0], to.y + to.height],
      ];
      fromPort = "right";
      toPort = "bottom";
      labelCorridor = { start: sideX, end: toCenter[0], y: clearanceY, gapStart, gapEnd };
    } else {
      const horizontal = Math.abs(toCenter[0] - fromCenter[0]) >= Math.abs(toCenter[1] - fromCenter[1]);
      if (horizontal) {
        const forward = toCenter[0] >= fromCenter[0];
        const start = [forward ? from.x + from.width : from.x, fromCenter[1]];
        const end = [forward ? to.x : to.x + to.width, toCenter[1]];
        const middle = Math.round((start[0] + end[0]) / 2);
        points = [start, [middle, start[1]], [middle, end[1]], end];
        fromPort = forward ? "right" : "left";
        toPort = forward ? "left" : "right";
      } else {
        const forward = toCenter[1] >= fromCenter[1];
        const start = [fromCenter[0], forward ? from.y + from.height : from.y];
        const end = [toCenter[0], forward ? to.y : to.y + to.height];
        const middle = Math.round((start[1] + end[1]) / 2);
        points = [start, [start[0], middle], [end[0], middle], end];
        fromPort = forward ? "bottom" : "top";
        toPort = forward ? "top" : "bottom";
      }
    }
    const obstacleRects = items
      .filter((item) => item.kind === "node" && item.id !== edge.from && item.id !== edge.to)
      .map((item) => rects.get(item.id));
    const routed = obstacleAvoidingRoute(from, to, obstacleRects, root, { points, fromPort, toPort });
    const allNodeRects = items.filter((item) => item.kind === "node").map((item) => rects.get(item.id));
    let labelPlacement = labelPlacementForPath(edge.label, routed.points, allNodeRects, root);
    if (!labelPlacement && edge.label && labelCorridor && labelCorridor.gapEnd - labelCorridor.gapStart >= 24) {
      const width = Math.min(160, Math.max(40, estimatedTextWidth(edge.label) + 12));
      labelPlacement = {
        x: Math.round((labelCorridor.start + labelCorridor.end - width) / 2),
        y: Math.round(labelCorridor.y - 11),
        width,
        height: 22,
      };
    }
    return {
      ...edge,
      points: routed.points,
      fromPort: routed.fromPort,
      toPort: routed.toPort,
      ...(labelPlacement ? { labelPlacement } : {}),
    };
  }
  return {
    version: 1,
    layoutVersion: "iac-layered-v7",
    width: root.width,
    height: root.height,
    items: items.map((item) => ({ ...item, ...rects.get(item.id) })),
    edges: edges.map(route),
  };
}

self.onmessage = (event) => {
  const requestId = event.data?.requestId;
  try {
    self.postMessage({ requestId, ok: true, layout: buildLayout(event.data?.graph) });
  } catch (error) {
    self.postMessage({
      requestId,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};
