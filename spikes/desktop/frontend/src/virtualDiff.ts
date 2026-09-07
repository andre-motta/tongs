export const DIFF_ROW_HEIGHT = 27;
export const DIFF_OVERSCAN = 8;

export type DiffWindow = {
  start: number;
  end: number;
  top: number;
  height: number;
};

export function getDiffWindow(
  length: number,
  scrollTop: number,
  viewportHeight: number,
): DiffWindow {
  const maxScrollTop = Math.max(0, length * DIFF_ROW_HEIGHT - viewportHeight);
  const boundedScrollTop = Math.min(Math.max(0, scrollTop), maxScrollTop);
  const start = Math.max(0, Math.floor(boundedScrollTop / DIFF_ROW_HEIGHT) - DIFF_OVERSCAN);
  const end = Math.min(
    length,
    Math.ceil((boundedScrollTop + viewportHeight) / DIFF_ROW_HEIGHT) + DIFF_OVERSCAN,
  );
  return {
    start,
    end,
    top: start * DIFF_ROW_HEIGHT,
    height: length * DIFF_ROW_HEIGHT,
  };
}
