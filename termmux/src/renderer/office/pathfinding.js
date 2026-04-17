/**
 * pathfinding.js — BFS tile pathfinding for office characters.
 * MIT License — ported from pablodelucca/pixel-agents
 */

/**
 * Find a shortest path from (startCol, startRow) to (endCol, endRow).
 * @param {number} startCol
 * @param {number} startRow
 * @param {number} endCol
 * @param {number} endRow
 * @param {boolean[][]} walkable - [row][col] array; true = passable
 * @returns {{col:number, row:number}[]} Steps after start, up to and including end.
 */
export function findPath(startCol, startRow, endCol, endRow, walkable) {
  if (startCol === endCol && startRow === endRow) return [];
  const rows = walkable.length;
  const cols = walkable[0]?.length ?? 0;
  if (!inBounds(endCol, endRow, cols, rows) || !walkable[endRow]?.[endCol]) return [];

  const visited = Array.from({ length: rows }, () => new Array(cols).fill(false));
  const parent  = Array.from({ length: rows }, () => new Array(cols).fill(null));
  const queue   = [{ col: startCol, row: startRow }];
  visited[startRow][startCol] = true;

  const DIRS = [
    { dc: 0,  dr: 1  },
    { dc: 0,  dr: -1 },
    { dc: 1,  dr: 0  },
    { dc: -1, dr: 0  },
  ];

  while (queue.length > 0) {
    const { col, row } = queue.shift();
    if (col === endCol && row === endRow) {
      const path = [];
      let cur = { col, row };
      while (cur.col !== startCol || cur.row !== startRow) {
        path.unshift(cur);
        cur = parent[cur.row][cur.col];
      }
      return path;
    }
    for (const { dc, dr } of DIRS) {
      const nc = col + dc, nr = row + dr;
      if (inBounds(nc, nr, cols, rows) && !visited[nr][nc] && walkable[nr][nc]) {
        visited[nr][nc] = true;
        parent[nr][nc] = { col, row };
        queue.push({ col: nc, row: nr });
      }
    }
  }
  return [];
}

function inBounds(col, row, cols, rows) {
  return col >= 0 && col < cols && row >= 0 && row < rows;
}
