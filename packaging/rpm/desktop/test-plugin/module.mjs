export function mount(root) {
  root.textContent = "Installed RPM plugin";
}

export function unmount(root) {
  root.replaceChildren();
}
