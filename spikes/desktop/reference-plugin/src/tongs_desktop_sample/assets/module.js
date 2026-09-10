export function mount(container, api) {
  const title = document.createElement('h2');
  title.textContent = 'Independently installed plugin';
  const input = document.createElement('input');
  input.value = 'Hello from desktop';
  input.setAttribute('aria-label', 'Plugin message');
  const button = document.createElement('button');
  button.textContent = 'Call Python plugin';
  const output = document.createElement('pre');
  output.setAttribute('aria-live', 'polite');
  button.onclick = async () => {
    button.disabled = true;
    try { output.textContent = JSON.stringify(await api.invoke('echo', {text: input.value}), null, 2); }
    catch (error) { output.textContent = String(error); }
    finally { button.disabled = false; }
  };
  container.append(title, input, button, output);
  return () => container.replaceChildren();
}
