/**
 * mapRenderer.js — Draws the office floor, walls, and furniture onto a canvas.
 * MIT License — concept ported from pablodelucca/pixel-agents
 */
import { TILE_SIZE, CANVAS_WIDTH, CANVAS_HEIGHT, TileType } from './types.js';

// ── Color palette ─────────────────────────────────────────────────────────────
const COLOR = {
  wall:        '#0d1117',
  wallTop:     '#21262d',
  floor:       '#161b22',
  floorGrid:   '#1c2128',
  deskTop:     '#4d3b1a',
  deskFront:   '#6b5228',
  deskHighlight:'#7d6035',
  chairSeat:   '#1f3a52',
  chairBack:   '#2d5278',
  monitor:     '#0d1117',
  monitorGlow: '#58a6ff',
  monitorStand:'#30363d',
  bossDesk:    '#3a2a0e',
  bossDeskFront:'#5a4020',
  bossBadge:   '#d29922',
};

// ── Tile drawing helpers ──────────────────────────────────────────────────────

function fillTile(ctx, col, row, color) {
  ctx.fillStyle = color;
  ctx.fillRect(col * TILE_SIZE, row * TILE_SIZE, TILE_SIZE, TILE_SIZE);
}

function drawFloorTile(ctx, col, row) {
  fillTile(ctx, col, row, COLOR.floor);
  // Subtle grid lines
  ctx.strokeStyle = COLOR.floorGrid;
  ctx.lineWidth = 0.5;
  ctx.strokeRect(col * TILE_SIZE + 0.5, row * TILE_SIZE + 0.5, TILE_SIZE - 1, TILE_SIZE - 1);
}

function drawWallTile(ctx, col, row) {
  fillTile(ctx, col, row, COLOR.wall);
  // Top highlight strip
  ctx.fillStyle = COLOR.wallTop;
  ctx.fillRect(col * TILE_SIZE, row * TILE_SIZE, TILE_SIZE, 2);
}

/**
 * Draw a worker desk spanning 2 tiles wide, anchored at (col, row).
 * The desk top is at row-1, the seat is at (col, row).
 */
function drawWorkerDesk(ctx, col, row) {
  const px = col * TILE_SIZE;
  const py = (row - 1) * TILE_SIZE; // desk surface is 1 tile above seat

  // Desk surface (1 tile tall, 2 tiles wide for visual weight)
  ctx.fillStyle = COLOR.deskTop;
  ctx.fillRect(px - TILE_SIZE / 2, py, TILE_SIZE * 2, TILE_SIZE);

  // Desk front panel
  ctx.fillStyle = COLOR.deskFront;
  ctx.fillRect(px - TILE_SIZE / 2, py + TILE_SIZE - 3, TILE_SIZE * 2, 3);

  // Highlight edge
  ctx.fillStyle = COLOR.deskHighlight;
  ctx.fillRect(px - TILE_SIZE / 2, py, TILE_SIZE * 2, 1);

  // Mini monitor
  const mx = px + 1;
  const my = py + 2;
  // Monitor body
  ctx.fillStyle = COLOR.monitor;
  ctx.fillRect(mx, my, 8, 6);
  // Monitor glow (screen content)
  ctx.fillStyle = COLOR.monitorGlow;
  ctx.fillRect(mx + 1, my + 1, 6, 4);
  // Screen scanline effect
  ctx.fillStyle = 'rgba(0,0,0,0.3)';
  for (let y = my + 1; y < my + 5; y += 2) {
    ctx.fillRect(mx + 1, y, 6, 1);
  }
  // Monitor stand
  ctx.fillStyle = COLOR.monitorStand;
  ctx.fillRect(mx + 3, my + 6, 2, 2);

  // Chair
  ctx.fillStyle = COLOR.chairSeat;
  ctx.fillRect(px + 2, row * TILE_SIZE + 2, TILE_SIZE - 4, TILE_SIZE - 6);
  ctx.fillStyle = COLOR.chairBack;
  ctx.fillRect(px + 2, row * TILE_SIZE + 2, TILE_SIZE - 4, 3);
}

/**
 * Draw the boss desk (wider, fancier) at (col, row).
 */
function drawBossDesk(ctx, col, row) {
  const px = col * TILE_SIZE;
  const py = (row - 1) * TILE_SIZE;

  // Wide desk surface (3 tiles wide)
  ctx.fillStyle = COLOR.bossDesk;
  ctx.fillRect(px - TILE_SIZE, py, TILE_SIZE * 3, TILE_SIZE);
  ctx.fillStyle = COLOR.bossDeskFront;
  ctx.fillRect(px - TILE_SIZE, py + TILE_SIZE - 4, TILE_SIZE * 3, 4);
  // Gold trim
  ctx.fillStyle = COLOR.bossBadge;
  ctx.fillRect(px - TILE_SIZE, py, TILE_SIZE * 3, 1);
  ctx.fillRect(px - TILE_SIZE, py + TILE_SIZE - 4, TILE_SIZE * 3, 1);

  // Name plate
  ctx.fillStyle = COLOR.bossBadge;
  ctx.fillRect(px + 1, py + TILE_SIZE - 8, 14, 4);

  // Boss monitor (larger)
  ctx.fillStyle = COLOR.monitor;
  ctx.fillRect(px - 2, py + 2, 12, 8);
  ctx.fillStyle = COLOR.monitorGlow;
  ctx.fillRect(px - 1, py + 3, 10, 6);
  ctx.fillStyle = 'rgba(88,166,255,0.4)';
  ctx.fillRect(px - 1, py + 3, 10, 6);

  // Chair
  ctx.fillStyle = '#1a3a5c';
  ctx.fillRect(px + 2, row * TILE_SIZE + 1, TILE_SIZE - 4, TILE_SIZE - 5);
  ctx.fillStyle = '#2a5a8c';
  ctx.fillRect(px + 2, row * TILE_SIZE + 1, TILE_SIZE - 4, 4);
}

// ── Map builder ───────────────────────────────────────────────────────────────

/**
 * Build the static tile map based on boss/worker seat positions.
 * @param {{col:number, row:number}} bossSeat
 * @param {{col:number, row:number}[]} workerSeats
 * @param {number} tilesX
 * @param {number} tilesY
 * @returns {{tileMap: string[][], walkable: boolean[][], wanderTiles: {col,row}[]}}
 */
export function buildOfficeMap(bossSeat, workerSeats, tilesX, tilesY) {
  // Initialize all tiles as floor
  const tileMap = Array.from({ length: tilesY }, () => new Array(tilesX).fill(TileType.FLOOR));

  // Wall border
  for (let c = 0; c < tilesX; c++) {
    tileMap[0][c] = TileType.WALL;
  }
  for (let r = 0; r < tilesY; r++) {
    tileMap[r][0] = TileType.WALL;
    tileMap[r][tilesX - 1] = TileType.WALL;
  }

  // Mark desk tiles (1 row above each seat)
  const deskSeats = [bossSeat, ...workerSeats];
  const deskTiles = new Set();
  for (const s of deskSeats) {
    if (s.row > 0) {
      tileMap[s.row - 1][s.col] = TileType.DESK;
      deskTiles.add(`${s.col},${s.row - 1}`);
      // Also mark adjacent desk tiles as non-walkable
      tileMap[s.row - 1][s.col - 1] && (tileMap[s.row - 1][s.col - 1] = TileType.DESK);
      tileMap[s.row - 1][s.col + 1] && (tileMap[s.row - 1][s.col + 1] = TileType.DESK);
    }
  }

  // Walkable: floor only (not wall, not desk, not seat row if blocked)
  const walkable = Array.from({ length: tilesY }, (_, r) =>
    Array.from({ length: tilesX }, (_, c) => tileMap[r][c] === TileType.FLOOR)
  );

  // Wander tiles: floor tiles in the middle aisles (cols 1-18, away from desks)
  const seatSet = new Set(deskSeats.map(s => `${s.col},${s.row}`));
  const wanderTiles = [];
  for (let r = 1; r < tilesY - 1; r++) {
    for (let c = 1; c < tilesX - 1; c++) {
      if (walkable[r][c] && !seatSet.has(`${c},${r}`) && !deskTiles.has(`${c},${r}`)) {
        wanderTiles.push({ col: c, row: r });
      }
    }
  }

  return { tileMap, walkable, wanderTiles };
}

// ── Render ────────────────────────────────────────────────────────────────────

/**
 * Draw the full office map (floor, walls, furniture) onto ctx.
 * @param {CanvasRenderingContext2D} ctx
 * @param {string[][]} tileMap
 * @param {{col:number,row:number}} bossSeat
 * @param {{col:number,row:number}[]} workerSeats
 */
export function renderMap(ctx, tileMap, bossSeat, workerSeats) {
  const rows = tileMap.length;
  const cols = tileMap[0]?.length ?? 0;

  // 1. Draw floor and walls
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      switch (tileMap[r][c]) {
        case TileType.WALL:  drawWallTile(ctx, c, r); break;
        case TileType.FLOOR: drawFloorTile(ctx, c, r); break;
        case TileType.DESK:  drawFloorTile(ctx, c, r); break; // floor under desk
      }
    }
  }

  // 2. Draw furniture (on top of floor tiles, z-sorted by row)
  drawBossDesk(ctx, bossSeat.col, bossSeat.row);
  for (const s of workerSeats) {
    drawWorkerDesk(ctx, s.col, s.row);
  }

  // 3. Draw small label above boss desk
  ctx.fillStyle = COLOR.bossBadge;
  ctx.font = '5px monospace';
  ctx.textAlign = 'center';
  ctx.fillText('BOSS', bossSeat.col * TILE_SIZE + TILE_SIZE / 2, (bossSeat.row - 1) * TILE_SIZE - 1);
  ctx.textAlign = 'left';
}
