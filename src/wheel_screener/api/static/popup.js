// Popup menus: <details data-popup> whose panel floats over the page. Pressing its button again
// closes it, and so does a press anywhere else on the page, or Escape.
// Delegated from the document, so it works on content htmx swaps in after the page loads.
let pressedOn = null;  // where the press began: selecting the typed name can end outside the panel
document.addEventListener("pointerdown", (event) => { pressedOn = event.target; });
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll("details[data-popup][open]")) {
    if (!menu.contains(event.target) && !menu.contains(pressedOn)) menu.open = false;
  }
  pressedOn = null;
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const menu of document.querySelectorAll("details[data-popup][open]")) {
    const inside = menu.contains(document.activeElement);
    menu.open = false;
    if (inside) menu.querySelector("summary").focus();
  }
});
