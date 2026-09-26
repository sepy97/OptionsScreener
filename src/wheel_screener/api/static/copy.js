// "Copy" buttons: <button data-copy="ID"> copies the value of the field with that id.
// Delegated from the document, so it works on content htmx swaps in after the page loads.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy);
  if (!field) return;
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(field.value);
    button.textContent = "Copied";
  } catch (err) {
    // no clipboard access (an old browser, or a page not served over https): select it instead
    field.focus();
    field.select();
    button.textContent = "Selected — copy it";
  }
  setTimeout(() => { button.textContent = label; }, 2000);
});
