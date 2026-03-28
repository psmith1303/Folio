// ---------------------------------------------------------------------------
// Theme toggle — dark/light mode with localStorage persistence
// ---------------------------------------------------------------------------

import { btnTheme } from "./dom.js";

export function initTheme() {
  // Migrate old localStorage key
  if (!localStorage.getItem("folio-theme") && localStorage.getItem("msv-theme")) {
    localStorage.setItem("folio-theme", localStorage.getItem("msv-theme"));
    localStorage.removeItem("msv-theme");
  }

  const savedTheme = localStorage.getItem("folio-theme");
  if (savedTheme === "light") {
    document.documentElement.classList.add("light");
    btnTheme.textContent = "Dark";
  }

  btnTheme.addEventListener("click", () => {
    const isLight = document.documentElement.classList.toggle("light");
    btnTheme.textContent = isLight ? "Dark" : "Light";
    localStorage.setItem("folio-theme", isLight ? "light" : "dark");
  });
}
