export interface CursorSearch {
  cursor?: string;
  history?: string[];
}

const MAX_CURSOR_HISTORY = 100;

export function validateCursorSearch(search: Record<string, unknown>): CursorSearch {
  const history = Array.isArray(search.history)
    ? search.history.filter((item): item is string => typeof item === "string").slice(-MAX_CURSOR_HISTORY)
    : undefined;
  return {
    cursor: typeof search.cursor === "string" ? search.cursor : undefined,
    history: history?.length ? history : undefined,
  };
}

export function advanceCursor(search: CursorSearch, cursor: string): CursorSearch {
  return {
    cursor,
    history: [...(search.history ?? []), search.cursor ?? ""].slice(-MAX_CURSOR_HISTORY),
  };
}

export function retreatCursor(search: CursorSearch): CursorSearch {
  const history = search.history ?? [];
  const previous = history.at(-1);
  const remaining = history.slice(0, -1);
  return {
    cursor: previous || undefined,
    history: remaining.length ? remaining : undefined,
  };
}

export function CursorPagination({
  page,
  hasNext,
  hasPrevious,
  label,
  onNext,
  onPrevious,
}: {
  page: number;
  hasNext: boolean;
  hasPrevious: boolean;
  label: string;
  onNext: () => void;
  onPrevious: () => void;
}) {
  return (
    <nav className="cursor-pagination" aria-label={`${label} bounded pagination`}>
      <button type="button" disabled={!hasPrevious} onClick={onPrevious}>← PREVIOUS BOUNDED PAGE</button>
      <span>BOUNDED PAGE {page}</span>
      <button type="button" disabled={!hasNext} onClick={onNext}>NEXT BOUNDED PAGE →</button>
    </nav>
  );
}
