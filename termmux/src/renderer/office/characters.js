/**
 * characters.js — Character FSM + per-frame update logic.
 * MIT License — ported from pablodelucca/pixel-agents
 */
import {
  CharacterState, Direction,
  TILE_SIZE, DIR_ROW,
  TYPE_FRAME_DURATION, WALK_FRAME_DURATION, WALK_SPEED_PPS,
  WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC,
  WANDER_MOVES_MIN, WANDER_MOVES_MAX,
  SEAT_REST_MIN_SEC, SEAT_REST_MAX_SEC,
} from './types.js';
import { findPath } from './pathfinding.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

function rand(min, max) { return min + Math.random() * (max - min); }
function randInt(min, max) { return min + Math.floor(Math.random() * (max - min + 1)); }

/** Pixel center of a tile. */
function tileCenter(col, row) {
  return { x: col * TILE_SIZE + TILE_SIZE / 2, y: row * TILE_SIZE + TILE_SIZE / 2 };
}

/** Direction from one tile to an adjacent tile. */
function dirBetween(fc, fr, tc, tr) {
  const dc = tc - fc, dr = tr - fr;
  if (dc > 0) return Direction.RIGHT;
  if (dc < 0) return Direction.LEFT;
  if (dr > 0) return Direction.DOWN;
  return Direction.UP;
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Create a new character object.
 * @param {number} id
 * @param {number} palette - 0..5; picks char_N.png
 * @param {{col:number,row:number,facingDir:string}|null} seat
 * @param {boolean} isActive - true = typing at desk, false = wandering
 * @param {string|null} tint - CSS rgba string for status tint overlay, or null
 * @returns {object}
 */
export function createCharacter(id, palette, seat, isActive, tint = null) {
  const col = seat ? seat.col : 1;
  const row = seat ? seat.row : 1;
  const center = tileCenter(col, row);
  return {
    id,
    palette,
    tint,
    isActive,
    seat: seat ? { ...seat } : null,
    state:      isActive ? CharacterState.TYPE : CharacterState.IDLE,
    dir:        seat ? seat.facingDir : Direction.DOWN,
    x:          center.x,
    y:          center.y,
    tileCol:    col,
    tileRow:    row,
    path:       [],
    moveProgress: 0,
    frame:      0,
    frameTimer: 0,
    wanderTimer: rand(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC),
    wanderCount: 0,
    wanderLimit: randInt(WANDER_MOVES_MIN, WANDER_MOVES_MAX),
    seatTimer:  0,
  };
}

/**
 * Update a character for one tick.
 * @param {object} ch
 * @param {number} dt - delta seconds
 * @param {boolean[][]} walkable - [row][col]
 * @param {{col:number,row:number}[]} wanderTiles - tiles the character may roam
 */
export function updateCharacter(ch, dt, walkable, wanderTiles) {
  ch.frameTimer += dt;

  switch (ch.state) {
    case CharacterState.TYPE: {
      if (ch.frameTimer >= TYPE_FRAME_DURATION) {
        ch.frameTimer -= TYPE_FRAME_DURATION;
        ch.frame = (ch.frame + 1) % 2;
      }
      // Seat timer: idle characters rest at desk briefly after wandering
      if (!ch.isActive && ch.seatTimer > 0) {
        ch.seatTimer -= dt;
        if (ch.seatTimer <= 0) {
          ch.seatTimer = 0;
          ch.state = CharacterState.IDLE;
          ch.frame = 0;
          ch.frameTimer = 0;
          ch.wanderTimer = rand(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC);
          ch.wanderCount = 0;
          ch.wanderLimit = randInt(WANDER_MOVES_MIN, WANDER_MOVES_MAX);
        }
        break;
      }
      if (!ch.isActive && ch.seatTimer <= 0) {
        ch.state = CharacterState.IDLE;
        ch.frame = 0;
        ch.frameTimer = 0;
        ch.wanderTimer = rand(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC);
      }
      break;
    }

    case CharacterState.IDLE: {
      ch.frame = 0;
      if (ch.isActive) {
        // Walk back to seat
        if (ch.seat) {
          const path = findPath(ch.tileCol, ch.tileRow, ch.seat.col, ch.seat.row, walkable);
          if (path.length > 0) {
            ch.path = path;
            ch.moveProgress = 0;
            ch.state = CharacterState.WALK;
            ch.frame = 0;
            ch.frameTimer = 0;
          } else {
            ch.state = CharacterState.TYPE;
            ch.dir = ch.seat.facingDir;
          }
        } else {
          ch.state = CharacterState.TYPE;
        }
        break;
      }

      ch.wanderTimer -= dt;
      if (ch.wanderTimer > 0) break;

      // Return to seat after enough wanders
      if (ch.wanderCount >= ch.wanderLimit && ch.seat) {
        const path = findPath(ch.tileCol, ch.tileRow, ch.seat.col, ch.seat.row, walkable);
        if (path.length > 0) {
          ch.path = path;
          ch.moveProgress = 0;
          ch.state = CharacterState.WALK;
          ch.frame = 0;
          ch.frameTimer = 0;
          break;
        }
      }

      // Wander to a random floor tile
      if (wanderTiles.length > 0) {
        const target = wanderTiles[Math.floor(Math.random() * wanderTiles.length)];
        const path = findPath(ch.tileCol, ch.tileRow, target.col, target.row, walkable);
        if (path.length > 0) {
          ch.path = path;
          ch.moveProgress = 0;
          ch.state = CharacterState.WALK;
          ch.frame = 0;
          ch.frameTimer = 0;
          ch.wanderCount++;
        }
      }
      ch.wanderTimer = rand(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC);
      break;
    }

    case CharacterState.WALK: {
      if (ch.frameTimer >= WALK_FRAME_DURATION) {
        ch.frameTimer -= WALK_FRAME_DURATION;
        ch.frame = (ch.frame + 1) % 4;
      }

      if (ch.path.length === 0) {
        // Arrived at destination
        const center = tileCenter(ch.tileCol, ch.tileRow);
        ch.x = center.x;
        ch.y = center.y;
        ch.frame = 0;
        ch.frameTimer = 0;

        if (ch.isActive && ch.seat &&
            ch.tileCol === ch.seat.col && ch.tileRow === ch.seat.row) {
          ch.state = CharacterState.TYPE;
          ch.dir = ch.seat.facingDir;
        } else if (!ch.isActive && ch.seat &&
                   ch.tileCol === ch.seat.col && ch.tileRow === ch.seat.row) {
          // Sit for a while before wandering again
          ch.state = CharacterState.TYPE;
          ch.dir = ch.seat.facingDir;
          ch.seatTimer = rand(SEAT_REST_MIN_SEC, SEAT_REST_MAX_SEC);
          ch.wanderCount = 0;
          ch.wanderLimit = randInt(WANDER_MOVES_MIN, WANDER_MOVES_MAX);
        } else {
          ch.state = CharacterState.IDLE;
          ch.wanderTimer = rand(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC);
        }
        break;
      }

      const next = ch.path[0];
      ch.dir = dirBetween(ch.tileCol, ch.tileRow, next.col, next.row);
      ch.moveProgress += (WALK_SPEED_PPS / TILE_SIZE) * dt;

      const from = tileCenter(ch.tileCol, ch.tileRow);
      const to   = tileCenter(next.col, next.row);
      const t    = Math.min(ch.moveProgress, 1);
      ch.x = from.x + (to.x - from.x) * t;
      ch.y = from.y + (to.y - from.y) * t;

      if (ch.moveProgress >= 1) {
        ch.tileCol = next.col;
        ch.tileRow = next.row;
        ch.x = to.x;
        ch.y = to.y;
        ch.path.shift();
        ch.moveProgress = 0;
      }

      // If activated while walking, re-path to seat
      if (ch.isActive && ch.seat) {
        const last = ch.path[ch.path.length - 1];
        if (!last || last.col !== ch.seat.col || last.row !== ch.seat.row) {
          const newPath = findPath(ch.tileCol, ch.tileRow, ch.seat.col, ch.seat.row, walkable);
          if (newPath.length > 0) {
            ch.path = newPath;
            ch.moveProgress = 0;
          }
        }
      }
      break;
    }
  }
}

/**
 * Get sprite sheet coordinates for the character's current frame.
 * @param {object} ch
 * @returns {{sx:number, sy:number, flipX:boolean}}
 *   sx/sy = pixel offset into the 112×96 PNG; flipX = mirror for LEFT direction.
 */
export function getSpriteCoords(ch) {
  const FRAME_W = 16, FRAME_H = 32;

  // Determine sheet row from direction
  const sheetRow = DIR_ROW[ch.dir] ?? 0;

  // Determine sheet column from state + frame
  let sheetCol;
  switch (ch.state) {
    case CharacterState.WALK:
      // Cycle: frame 0→col 0, 1→col 1, 2→col 2, 3→col 1
      sheetCol = [0, 1, 2, 1][ch.frame % 4];
      break;
    case CharacterState.TYPE:
      // Cycle: frame 0→col 3, 1→col 4
      sheetCol = 3 + (ch.frame % 2);
      break;
    case CharacterState.IDLE:
    default:
      sheetCol = 1; // neutral standing frame
  }

  return {
    sx:    sheetCol * FRAME_W,
    sy:    sheetRow * FRAME_H,
    flipX: ch.dir === Direction.LEFT,
  };
}
