/**
 * gameLoop.js — requestAnimationFrame game loop with delta time.
 * MIT License — ported from pablodelucca/pixel-agents
 */
import { MAX_DELTA_SEC } from './types.js';

/**
 * Start the game loop.
 * @param {HTMLCanvasElement} canvas
 * @param {{ update: (dt:number)=>void, render: (ctx:CanvasRenderingContext2D)=>void }} callbacks
 * @returns {()=>void} Stop function — call it to cancel the loop.
 */
export function startGameLoop(canvas, { update, render }) {
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;

  let lastTime = 0;
  let rafId    = 0;
  let stopped  = false;

  const frame = (time) => {
    if (stopped) return;
    const dt = lastTime === 0 ? 0 : Math.min((time - lastTime) / 1000, MAX_DELTA_SEC);
    lastTime = time;

    update(dt);

    ctx.imageSmoothingEnabled = false;
    render(ctx);

    rafId = requestAnimationFrame(frame);
  };

  rafId = requestAnimationFrame(frame);

  return () => {
    stopped = true;
    cancelAnimationFrame(rafId);
  };
}
