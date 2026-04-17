/**
 * characterRenderer.js — Draws character sprites onto the canvas.
 * MIT License — concept ported from pablodelucca/pixel-agents
 */
import { SPRITE_FRAME_W, SPRITE_FRAME_H, PALETTE_FALLBACK } from './types.js';
import { getSpriteCoords } from './characters.js';

// Preloaded sprite images (indexed 0-5, matching char_0.png .. char_5.png)
const spriteImages = new Array(6).fill(null);
let spritesLoaded = false;

/**
 * Load all 6 character sprite sheets.
 * @param {string} basePath - base URL (e.g. '/assets/characters')
 * @returns {Promise<void>}
 */
export async function loadSprites(basePath) {
  const promises = [];
  for (let i = 0; i < 6; i++) {
    promises.push(new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { spriteImages[i] = img; resolve(); };
      img.onerror = () => { spriteImages[i] = null; resolve(); };
      img.src = `${basePath}/char_${i}.png`;
    }));
  }
  await Promise.all(promises);
  spritesLoaded = true;
}

/**
 * Draw all characters onto ctx, z-sorted by Y position (painter's algorithm).
 * @param {CanvasRenderingContext2D} ctx
 * @param {object[]} characters
 */
export function renderCharacters(ctx, characters) {
  // Sort by y so characters further down overlap those higher up
  const sorted = [...characters].sort((a, b) => a.y - b.y);
  for (const ch of sorted) {
    drawCharacter(ctx, ch);
  }
}

function drawCharacter(ctx, ch) {
  const { sx, sy, flipX } = getSpriteCoords(ch);

  // Sprite destination: character centered horizontally on x, feet at y + 8
  const dx = Math.round(ch.x - SPRITE_FRAME_W / 2);
  const dy = Math.round(ch.y - SPRITE_FRAME_H + 8);
  const dw = SPRITE_FRAME_W;
  const dh = SPRITE_FRAME_H;

  ctx.save();

  if (flipX) {
    // Mirror horizontally around the sprite center
    ctx.translate(dx + dw, 0);
    ctx.scale(-1, 1);
  }

  const drawX = flipX ? 0 : dx;

  const img = spriteImages[ch.palette % 6];
  if (img) {
    // Draw pixel-art sprite (disable smoothing for crisp pixels)
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, sx, sy, SPRITE_FRAME_W, SPRITE_FRAME_H, drawX, dy, dw, dh);
  } else {
    // Fallback: colored silhouette
    const color = PALETTE_FALLBACK[ch.palette % 6];
    // Body
    ctx.fillStyle = color;
    ctx.fillRect(drawX + 3, dy + 14, 10, 14);
    // Head
    ctx.fillRect(drawX + 4, dy + 8, 8, 8);
  }

  // ── Status tint overlay ─────────────────────────────────────────────────────
  if (ch.tint) {
    ctx.globalCompositeOperation = 'source-atop';
    ctx.fillStyle = ch.tint;
    ctx.fillRect(drawX, dy, dw, dh);
    ctx.globalCompositeOperation = 'source-over';
  }

  ctx.restore();

  // ── Name tag / status bubble above character ────────────────────────────────
  if (ch.label) {
    const tagX = Math.round(ch.x);
    const tagY = dy - 2;
    ctx.font = '5px monospace';
    ctx.textAlign = 'center';
    ctx.fillStyle = 'rgba(0,0,0,0.7)';
    const tw = ctx.measureText(ch.label).width;
    ctx.fillRect(tagX - tw / 2 - 1, tagY - 6, tw + 2, 7);
    ctx.fillStyle = ch.tint ? ch.tint.replace(/[\d.]+\)$/, '1)') : '#c9d1d9';
    ctx.fillText(ch.label, tagX, tagY);
    ctx.textAlign = 'left';
  }
}
