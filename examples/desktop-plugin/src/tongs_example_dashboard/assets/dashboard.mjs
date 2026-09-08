const DASHBOARD_EVENT = "refreshed";
const REFRESH_METHOD = "refresh";

function text(element, value) {
  element.textContent = String(value);
}

function renderSnapshot(elements, snapshot) {
  const summary = snapshot && typeof snapshot === "object" ? snapshot.summary : null;
  const reviews = snapshot && Array.isArray(snapshot.reviews) ? snapshot.reviews : [];
  const counts = summary && typeof summary === "object" ? summary : {};
  text(elements.openReviews, counts.open_reviews ?? 0);
  text(elements.waitingOnMe, counts.waiting_on_me ?? 0);
  text(elements.ciPassing, counts.ci_passing ?? 0);
  elements.reviews.replaceChildren();
  for (const review of reviews) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    text(title, review.title ?? "Untitled review");
    const metadata = document.createElement("span");
    text(metadata, `${review.repository ?? "unknown repository"} · ${review.status ?? "unknown"}`);
    item.append(title, metadata);
    elements.reviews.append(item);
  }
}

export function mount(container, api) {
  let mounted = true;
  let generation = 0;
  const heading = document.createElement("h2");
  text(heading, "Example dashboard");

  const description = document.createElement("p");
  text(description, "Deterministic example data for the Tongs desktop plugin SDK.");

  const location = document.createElement("p");
  location.className = "example-dashboard-location";
  text(location, `Location: ${api.currentLocation().navigation_id}`);

  const summary = document.createElement("section");
  summary.className = "example-dashboard-summary";
  summary.tabIndex = 0;
  summary.setAttribute("aria-label", "Review summary");
  const summaryTitle = document.createElement("h3");
  text(summaryTitle, "Summary");
  const openReviews = document.createElement("span");
  const waitingOnMe = document.createElement("span");
  const ciPassing = document.createElement("span");
  for (const [label, value] of [["Open reviews", openReviews], ["Waiting on me", waitingOnMe], ["CI passing", ciPassing]]) {
    const card = document.createElement("div");
    const cardLabel = document.createElement("span");
    text(cardLabel, label);
    card.append(cardLabel, value);
    summary.append(card);
  }
  summary.prepend(summaryTitle);

  const reviews = document.createElement("ul");
  reviews.className = "example-dashboard-reviews";
  const navigate = document.createElement("button");
  navigate.type = "button";
  text(navigate, "Open dashboard");
  const onNavigate = () => {
    if (mounted && !api.signal.aborted) api.navigate("dashboard");
  };
  navigate.addEventListener("click", onNavigate);
  const refresh = document.createElement("button");
  refresh.type = "button";
  text(refresh, "Refresh dashboard");
  const onRefresh = async () => {
    if (!mounted || api.signal.aborted) return;
    const requestGeneration = ++generation;
    refresh.disabled = true;
    try {
      const snapshot = await api.invoke(REFRESH_METHOD, {});
      if (!mounted || api.signal.aborted || generation !== requestGeneration) return;
      renderSnapshot(elements, snapshot);
      api.notify("Example dashboard refreshed", "information");
    } catch {
      if (!mounted || api.signal.aborted || generation !== requestGeneration) return;
      api.notify("Example dashboard refresh failed", "error");
    } finally {
      if (mounted && !api.signal.aborted && generation === requestGeneration) {
        refresh.disabled = false;
      }
    }
  };
  refresh.addEventListener("click", onRefresh);

  const elements = { openReviews, waitingOnMe, ciPassing, reviews };
  const unsubscribeEvent = api.on(DASHBOARD_EVENT, (snapshot) => renderSnapshot(elements, snapshot));
  const onAbort = () => {
    generation += 1;
    refresh.disabled = true;
  };
  api.signal.addEventListener("abort", onAbort, { once: true });
  container.append(heading, description, location, summary, reviews, navigate, refresh);
  const unbindSummary = api.bindFocusTarget("summary", summary);
  renderSnapshot(elements, { summary: {}, reviews: [] });

  return () => {
    if (!mounted) return;
    mounted = false;
    generation += 1;
    navigate.removeEventListener("click", onNavigate);
    refresh.removeEventListener("click", onRefresh);
    unsubscribeEvent();
    unbindSummary();
    api.signal.removeEventListener("abort", onAbort);
    container.replaceChildren();
  };
}
