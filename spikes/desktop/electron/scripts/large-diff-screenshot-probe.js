(async () => {
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const until = async (predicate, description) => {
    const deadline = performance.now() + 12000;
    while (performance.now() < deadline) {
      const result = predicate();
      if (result) return result;
      await pause(30);
    }
    throw new Error(`UI probe timed out: ${description}`);
  };
  const cards = await until(
    () => [...document.querySelectorAll(".review-card")],
    "review inbox",
  );
  const large = cards.find((node) => node.textContent.includes("20,000"));
  if (!large) throw new Error("Large fixture review missing");
  large.click();
  await until(
    () => document.querySelector(".diff-stat")?.textContent.includes("20,000"),
    "20,000-line diff",
  );
  const split = document.querySelector('[aria-label="Side-by-side diff view"]');
  if (!split) throw new Error("Split diff control missing");
  split.click();
  await until(() => document.querySelector(".split-row"), "split diff rows");
  const scroller = document.querySelector(".diff-scroll");
  scroller.scrollTop = scroller.scrollHeight;
  scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
  await until(
    () =>
      document
        .querySelector(".diff-window")
        ?.textContent.includes("render_review_item(19999,"),
    "last large-diff row",
  );
  await new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(resolve)),
  );
  await pause(100);
  const renderedRows = document.querySelectorAll(".diff-row").length;
  if (renderedRows < 1 || renderedRows >= 160) {
    throw new Error("Large diff DOM is not bounded");
  }
  const finalRow = [...document.querySelectorAll(".diff-row")].find((row) =>
    row.textContent.includes("render_review_item(19999,"),
  );
  const rowBounds = finalRow?.getBoundingClientRect();
  const scrollBounds = scroller.getBoundingClientRect();
  if (
    !rowBounds ||
    rowBounds.height <= 0 ||
    rowBounds.bottom <= scrollBounds.top ||
    rowBounds.top >= scrollBounds.bottom
  ) {
    throw new Error("Last large-diff row is not painted in the visible viewport");
  }
  return {
    ok: true,
    large_lines: 20000,
    split_diff: true,
    reached_last_row: true,
    rendered_rows: renderedRows,
  };
})()
