import { useEffect, useState, type ReactNode } from "react";

export interface WindowedListProps<T> {
  readonly items: readonly T[];
  readonly pageSize: number;
  readonly targetIndex?: number;
  readonly renderItem: (item: T, index: number) => ReactNode;
}

export function WindowedList<T>({
  items,
  pageSize,
  targetIndex = 0,
  renderItem,
}: WindowedListProps<T>): ReactNode {
  const [start, setStart] = useState(
    Math.max(0, Math.floor(targetIndex / pageSize) * pageSize),
  );
  useEffect(
    () => setStart(Math.max(0, Math.floor(targetIndex / pageSize) * pageSize)),
    [pageSize, targetIndex],
  );
  const boundedStart = Math.min(start, Math.max(0, items.length - 1));
  const end = Math.min(items.length, boundedStart + pageSize);
  return (
    <>
      <div className="windowed-items" role="list">
        {items
          .slice(boundedStart, end)
          .map((item, offset) => renderItem(item, boundedStart + offset))}
      </div>
      {items.length > pageSize && (
        <nav className="window-controls" aria-label="Diff row windows">
          <button
            className="button button-secondary"
            disabled={boundedStart === 0}
            onClick={() => setStart(Math.max(0, boundedStart - pageSize))}
          >
            Previous rows
          </button>
          <span className="window-position">
            {boundedStart + 1}-{end} of {items.length}
          </span>
          <button
            className="button button-secondary"
            disabled={end === items.length}
            onClick={() => setStart(end)}
          >
            Next rows
          </button>
        </nav>
      )}
    </>
  );
}
