/**
 * types.js — Constants and state enums for the Agent Office game engine.
 * MIT License — concept ported from pablodelucca/pixel-agents
 */

export const TILE_SIZE = 16;
export const CANVAS_WIDTH = 320;   // 20 tiles wide
export const CANVAS_HEIGHT = 240;  // 15 tiles tall
export const TILES_X = CANVAS_WIDTH / TILE_SIZE;   // 20
export const TILES_Y = CANVAS_HEIGHT / TILE_SIZE;  // 15
export const MAX_DELTA_SEC = 0.1;

/** Character animation states */
export const CharacterState = Object.freeze({
  IDLE: 'IDLE',
  WALK: 'WALK',
  TYPE: 'TYPE',
});

/** Facing directions */
export const Direction = Object.freeze({
  DOWN:  'DOWN',
  UP:    'UP',
  LEFT:  'LEFT',
  RIGHT: 'RIGHT',
});

// ── Sprite sheet layout (112×96 PNG, 7 cols × 3 rows) ─────────────────────────
export const SPRITE_FRAME_W = 16;   // each frame is 16px wide
export const SPRITE_FRAME_H = 32;   // each frame is 32px tall
// Column indices within the sheet:
//   0,1,2 = walk frames  (walk cycle: 0→1→2→1)
//   3,4   = typing frames (type cycle: 3→4)
//   5,6   = reading frames (not used, same as typing for our purposes)
// Row indices:
//   0 = DOWN, 1 = UP, 2 = RIGHT  (LEFT = RIGHT mirrored)
export const DIR_ROW = Object.freeze({
  [Direction.DOWN]:  0,
  [Direction.UP]:    1,
  [Direction.RIGHT]: 2,
  [Direction.LEFT]:  2,  // mirrored
});

// ── Timing constants ──────────────────────────────────────────────────────────
export const TYPE_FRAME_DURATION  = 0.28;  // seconds per typing frame
export const WALK_FRAME_DURATION  = 0.14;  // seconds per walk frame
export const WALK_SPEED_PPS       = 48;    // pixels per second while walking
export const WANDER_PAUSE_MIN_SEC = 1.5;
export const WANDER_PAUSE_MAX_SEC = 4.0;
export const WANDER_MOVES_MIN     = 2;
export const WANDER_MOVES_MAX     = 5;
export const SEAT_REST_MIN_SEC    = 2.0;
export const SEAT_REST_MAX_SEC    = 5.0;

// ── Palette fallback colors (when PNG fails to load) ─────────────────────────
export const PALETTE_FALLBACK = [
  '#58a6ff', // blue
  '#3fb950', // green
  '#d29922', // yellow
  '#f85149', // red
  '#bc8cff', // purple
  '#39d0d8', // cyan
];

// ── Office layout: fixed seat positions (tile col/row) ───────────────────────
// Canvas is 20×15 tiles.
// Boss desk at top center; workers in a 2-col grid below.
export const BOSS_SEAT = { col: 9, row: 2, facingDir: Direction.DOWN };

export const WORKER_SEATS = [
  // Row 1 desks  (desk tile is 1 row above the seat tile)
  { col: 4,  row: 5,  facingDir: Direction.DOWN },  // left col, row 1
  { col: 14, row: 5,  facingDir: Direction.DOWN },  // right col, row 1
  // Row 2 desks
  { col: 4,  row: 8,  facingDir: Direction.DOWN },
  { col: 14, row: 8,  facingDir: Direction.DOWN },
  // Row 3 desks
  { col: 4,  row: 11, facingDir: Direction.DOWN },
  { col: 14, row: 11, facingDir: Direction.DOWN },
  // Row 4 desks
  { col: 4,  row: 14, facingDir: Direction.DOWN },
  { col: 14, row: 14, facingDir: Direction.DOWN },
];
// Max visible tasks = WORKER_SEATS.length = 8
export const MAX_VISIBLE_TASKS = WORKER_SEATS.length;

// ── Map tile types ────────────────────────────────────────────────────────────
export const TileType = Object.freeze({
  WALL:  'W',
  FLOOR: 'F',
  DESK:  'D',
});
