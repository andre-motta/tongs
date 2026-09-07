async function loadFixture() {
  const status = document.querySelector("#status");
  const output = document.querySelector("#result");
  try {
    const [health, reviews, plugins] = await Promise.all([
      window.tongs.invoke("health"),
      window.tongs.invoke("list_reviews"),
      window.tongs.invoke("list_plugins"),
    ]);
    status.textContent = "Electron bridge and Python sidecar ready";
    output.textContent = JSON.stringify({ health, reviews, plugins }, null, 2);
    document.body.dataset.tongsReady = "true";
  } catch (error) {
    status.textContent = `Adapter failed: ${error.message}`;
  }
}

if (window.tongs) loadFixture();
else window.addEventListener("tongs-ready", loadFixture, { once: true });
