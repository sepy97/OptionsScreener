// "Copy" buttons: <button data-copy="ID"> copies the value of the field with that id, or the text
// of the element with that id when it isn't a field (an order ticket).
// Delegated from the document, so it works on content htmx swaps in after the page loads.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy);
  if (!field) return;
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText("value" in field ? field.value : field.innerText);
    button.textContent = "Copied";
  } catch (err) {
    // no clipboard access (an old browser, or a page not served over https): select it instead
    if ("select" in field) {
      field.focus();
      field.select();
    } else {
      const range = document.createRange();
      range.selectNodeContents(field);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
    button.textContent = "Selected — copy it";
  }
  setTimeout(() => { button.textContent = label; }, 2000);
});
