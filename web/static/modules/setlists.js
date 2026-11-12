// ---------------------------------------------------------------------------
// Setlists — list, detail, drag-and-drop, save, playback
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  setlistBody, setlistStatus, setlistDetailActions, setlistDetailName,
  setlistSongsBody, btnNewSetlist, btnRenameSetlist, btnDeleteSetlist,
  btnAddSong, btnPlaySetlist, btnAddSetlistRef,
  btnToggleShuffle,
  setlistNameDialog, setlistNameDialogTitle, setlistNameInput, setlistNameCancel,
  songPickerDialog, songSearch, songPickerList, songStart, songEnd,
  songPickerCancel, songPickerAdd,
  setlistRefPickerDialog, setlistRefPickerList, setlistRefPickerCancel,
  titleDisplay,
} from "./dom.js";
import { api } from "./api.js";
import { esc, explicitStartPage } from "./utils.js";
import { showView } from "./views.js";
import { openSetlistSong, openScore } from "./viewer.js";
import { searchScores } from "./library-filter.js";
import {
  CACHE_AVAILABLE, cacheButtonHtml, onCacheButtonClick, refreshCacheStatus,
  syncOfflineSet,
} from "./cache.js";

// ---------------------------------------------------------------------------
// Setlist list
// ---------------------------------------------------------------------------

export async function loadSetlists() {
  try {
    const data = await api("/api/setlists");
    renderSetlistList(data.setlists);
    setlistStatus.textContent = `${data.setlists.length} setlists`;
    const s = getState();
    if (s.editingSetlistName) {
      openSetlistDetail(s.editingSetlistName);
    }
  } catch (err) {
    setlistStatus.textContent = `Error: ${err.message}`;
  }
}

function renderSetlistList(setlists) {
  const s = getState();
  setlistBody.innerHTML = "";
  for (const sl of setlists) {
    const tr = document.createElement("tr");
    if (sl.name === s.editingSetlistName) {
      tr.classList.add("selected-setlist");
    }
    const countLabel = sl.count === sl.flat_count
      ? `${sl.count}`
      : `${sl.count} (${sl.flat_count} songs)`;
    const shuffleMark = sl.shuffle
      ? ` <span class="shuffle-mark" title="Plays in random order">&#x21C4;</span>`
      : "";
    tr.innerHTML = `
      <td>${esc(sl.name)}${shuffleMark}</td>
      <td>${countLabel}</td>
      <td class="setlist-actions">
        <button class="small-btn del-btn" title="Delete">&#10005;</button>
      </td>
    `;
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".del-btn")) return;
      openSetlistDetail(sl.name);
    });
    tr.addEventListener("dblclick", () => startSetlistPlayback(sl.name));
    tr.querySelector(".del-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSetlist(sl.name);
    });
    setlistBody.appendChild(tr);
  }
}

async function deleteSetlist(name) {
  if (!confirm(`Delete setlist "${name}"?`)) return;
  try {
    await api(`/api/setlists/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (err) {
    console.error("Failed to delete setlist:", err);
    return;
  }
  syncOfflineSet();  // release the pins it held
  const s = getState();
  if (s.editingSetlistName === name) {
    s.editingSetlistName = null;
    s.editingSetlistItems = [];
    s.editingSetlistShuffle = false;
    renderSetlistDetail();
  }
  loadSetlists();
}

// ---------------------------------------------------------------------------
// Setlist detail
// ---------------------------------------------------------------------------

export async function openSetlistDetail(name) {
  const s = getState();
  try {
    const data = await api(`/api/setlists/${encodeURIComponent(name)}`);
    s.editingSetlistName = data.name;
    s.editingSetlistItems = data.items || [];
    s.editingSetlistShuffle = !!data.shuffle;
    renderSetlistDetail();
    setlistBody.querySelectorAll("tr").forEach((row) => {
      row.classList.toggle("selected-setlist",
        row.querySelector("td").textContent === name);
    });
  } catch (err) {
    console.error("Failed to load setlist:", err);
  }
}

function renderSetlistDetail() {
  const s = getState();
  setlistSongsBody.innerHTML = "";

  if (!s.editingSetlistName) {
    setlistDetailName.textContent = "Select a setlist";
    setlistDetailActions.classList.add("hidden");
    return;
  }

  setlistDetailName.textContent = s.editingSetlistName;
  setlistDetailActions.classList.remove("hidden");
  btnToggleShuffle.classList.toggle("active", s.editingSetlistShuffle);
  btnToggleShuffle.setAttribute(
    "aria-pressed", s.editingSetlistShuffle ? "true" : "false",
  );

  let dragSrcIndex = null;

  s.editingSetlistItems.forEach((item, i) => {
    const tr = document.createElement("tr");
    tr.draggable = true;
    tr.dataset.index = i;

    if (item.type === "setlist_ref") {
      const refName = item.setlist_name || "";
      const label = item.exists === false
        ? `${esc(refName)} <span class="missing-ref">(missing)</span>`
        : `${esc(refName)} (${item.flat_count ?? "?"} songs)`;
      tr.classList.add("setlist-ref-row");
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td colspan="5" class="setlist-ref-label">&#9654; Setlist: ${label}</td>
        <td class="song-actions">
          <button class="small-btn up-btn" title="Move up" ${i === 0 ? "disabled" : ""}>&#8593;</button>
          <button class="small-btn down-btn" title="Move down" ${i === s.editingSetlistItems.length - 1 ? "disabled" : ""}>&#8595;</button>
          <button class="small-btn del-btn" title="Remove">&#10005;</button>
        </td>
      `;
    } else {
      const startPage = item.start_page || 1;
      tr.dataset.filepath = item.path;
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td>${esc(item.composer || "")}</td>
        <td>${esc(item.title || "")}</td>
        <td><input type="number" class="page-input start-pg" min="1" value="${startPage}"></td>
        <td><input type="number" class="page-input end-pg" min="0" value="${item.end_page || 0}"></td>
        <td class="cache-col">${CACHE_AVAILABLE ? cacheButtonHtml(item.path) : ""}</td>
        <td class="song-actions">
          <button class="small-btn open-btn" title="Open at page ${startPage}">&#9655;</button>
          <button class="small-btn up-btn" title="Move up" ${i === 0 ? "disabled" : ""}>&#8593;</button>
          <button class="small-btn down-btn" title="Move down" ${i === s.editingSetlistItems.length - 1 ? "disabled" : ""}>&#8595;</button>
          <button class="small-btn del-btn" title="Remove">&#10005;</button>
        </td>
      `;

      tr.querySelector(".start-pg").addEventListener("change", (e) => {
        item.start_page = parseInt(e.target.value, 10) || 1;
        saveSetlistItems();
      });
      tr.querySelector(".end-pg").addEventListener("change", (e) => {
        const val = parseInt(e.target.value, 10) || 0;
        item.end_page = val === 0 ? null : val;
        saveSetlistItems();
      });
      tr.querySelector(".open-btn").addEventListener("click", () => {
        openScore(
          {
            filepath: item.path,
            title: item.title || "",
            composer: item.composer || "",
          },
          { startPage: explicitStartPage(item) },
        );
      });
    }

    tr.querySelector(".up-btn").addEventListener("click", () => moveSong(i, -1));
    tr.querySelector(".down-btn").addEventListener("click", () => moveSong(i, 1));
    tr.querySelector(".del-btn").addEventListener("click", () => removeSong(i));

    // Drag-and-drop reorder
    tr.addEventListener("dragstart", (e) => {
      dragSrcIndex = i;
      tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    tr.addEventListener("dragend", () => {
      tr.classList.remove("dragging");
      setlistSongsBody.querySelectorAll("tr").forEach(
        (r) => r.classList.remove("drag-over")
      );
    });
    tr.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setlistSongsBody.querySelectorAll("tr").forEach(
        (r) => r.classList.remove("drag-over")
      );
      tr.classList.add("drag-over");
    });
    tr.addEventListener("drop", (e) => {
      e.preventDefault();
      const destIndex = parseInt(tr.dataset.index, 10);
      if (dragSrcIndex !== null && dragSrcIndex !== destIndex) {
        const moved = s.editingSetlistItems.splice(dragSrcIndex, 1)[0];
        s.editingSetlistItems.splice(destIndex, 0, moved);
        saveSetlistItems();
        renderSetlistDetail();
      }
    });

    setlistSongsBody.appendChild(tr);
  });

  if (CACHE_AVAILABLE) refreshCacheStatus(setlistSongsBody);
}

function moveSong(index, direction) {
  const s = getState();
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= s.editingSetlistItems.length) return;
  [s.editingSetlistItems[index], s.editingSetlistItems[newIndex]] =
    [s.editingSetlistItems[newIndex], s.editingSetlistItems[index]];
  saveSetlistItems();
  renderSetlistDetail();
}

function removeSong(index) {
  const s = getState();
  s.editingSetlistItems.splice(index, 1);
  saveSetlistItems();
  renderSetlistDetail();
}

async function saveSetlistItems() {
  const s = getState();
  if (!s.editingSetlistName) return;
  try {
    await api(`/api/setlists/${encodeURIComponent(s.editingSetlistName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: s.editingSetlistItems }),
    });
    syncOfflineSet();  // pin added songs, release removed ones
    // Refresh sidebar counts
    const data = await api("/api/setlists");
    renderSetlistList(data.setlists);
    setlistStatus.textContent = `${data.setlists.length} setlists`;
  } catch (err) {
    console.error("Failed to save setlist:", err);
  }
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------

export async function startSetlistPlayback(name) {
  const s = getState();
  try {
    const data = await api(`/api/setlists/${encodeURIComponent(name)}/playback`);
    if (data.songs.length === 0) return;
    s.returnView = s.currentView;
    s.setlistPlayback = { name, songs: data.songs, index: 0 };
    showView("viewer");
    openSetlistSong(0);
  } catch (err) {
    console.error("Failed to start playback:", err);
  }
}

// ---------------------------------------------------------------------------
// Add current score to setlist (from viewer)
// ---------------------------------------------------------------------------

export async function addCurrentScoreToSetlist(setlistName, startPage, endPage) {
  const s = getState();
  try {
    const data = await api(`/api/setlists/${encodeURIComponent(setlistName)}`);
    const items = data.items || [];
    items.push({
      type: "song",
      path: s.currentScore.filepath,
      title: s.currentScore.title || "",
      composer: s.currentScore.composer || "",
      start_page: startPage,
      end_page: endPage,
    });
    await api(`/api/setlists/${encodeURIComponent(setlistName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    syncOfflineSet();  // pin the song just added
    titleDisplay.textContent += ` \u2014 added to ${setlistName}`;
    setTimeout(() => {
      if (s.currentScore) {
        titleDisplay.textContent = `${s.currentScore.composer} \u2014 ${s.currentScore.title}`;
      }
    }, 2000);
  } catch (err) {
    console.error("Failed to add to setlist:", err);
  }
}

// ---------------------------------------------------------------------------
// Song picker
// ---------------------------------------------------------------------------

function renderSongPicker(query) {
  const s = getState();
  if (!s.libraryLoaded) {
    // Not "no matches": the library itself couldn't be fetched (yet).
    songPickerList.innerHTML = `<p style="color:#f88">Library not loaded</p>`;
    return;
  }
  songPickerList.innerHTML = "";
  for (const sc of searchScores(s.allScores, query)) {
    const div = document.createElement("div");
    div.className = "picker-item";
    div.textContent = `${sc.composer} \u2014 ${sc.title}`;
    div.addEventListener("click", () => {
      songPickerList.querySelectorAll(".picker-item").forEach(
        (el) => el.classList.remove("selected")
      );
      div.classList.add("selected");
      s.pickerSelectedScore = sc;
      songPickerAdd.disabled = false;
    });
    songPickerList.appendChild(div);
  }
}

// ---------------------------------------------------------------------------
// Init event listeners
// ---------------------------------------------------------------------------

export function initSetlistEvents() {
  const s = getState();

  setlistSongsBody.addEventListener("click", onCacheButtonClick);

  // New setlist
  btnNewSetlist.addEventListener("click", () => {
    s.setlistNameMode = "create";
    setlistNameDialogTitle.textContent = "New Setlist";
    setlistNameInput.value = "";
    setlistNameDialog.showModal();
    setlistNameInput.focus();
  });

  // Delete current setlist (from detail toolbar)
  btnDeleteSetlist.addEventListener("click", () => {
    if (!s.editingSetlistName) return;
    deleteSetlist(s.editingSetlistName);
  });

  // Toggle shuffle on/off for current setlist
  btnToggleShuffle.addEventListener("click", async () => {
    if (!s.editingSetlistName) return;
    const next = !s.editingSetlistShuffle;
    try {
      const resp = await api(
        `/api/setlists/${encodeURIComponent(s.editingSetlistName)}/shuffle`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shuffle: next }),
        },
      );
      s.editingSetlistShuffle = !!resp.shuffle;
      renderSetlistDetail();
      loadSetlists();
    } catch (err) {
      console.error("Failed to toggle shuffle:", err);
    }
  });

  // Rename setlist
  btnRenameSetlist.addEventListener("click", () => {
    s.setlistNameMode = "rename";
    setlistNameDialogTitle.textContent = "Rename Setlist";
    setlistNameInput.value = s.editingSetlistName || "";
    setlistNameDialog.showModal();
    setlistNameInput.focus();
  });

  setlistNameCancel.addEventListener("click", () => setlistNameDialog.close());

  setlistNameDialog.addEventListener("close", async () => {
    if (setlistNameDialog.returnValue !== "ok") return;
    const name = setlistNameInput.value.trim();
    if (!name) return;

    try {
      if (s.setlistNameMode === "create") {
        await api("/api/setlists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        loadSetlists();
      } else if (s.setlistNameMode === "rename" && s.editingSetlistName) {
        await api(`/api/setlists/${encodeURIComponent(s.editingSetlistName)}/rename`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_name: name }),
        });
        s.editingSetlistName = name;
        setlistDetailName.textContent = name;
        loadSetlists();
      }
    } catch (err) {
      console.error("Setlist name operation failed:", err);
    }
  });

  // Add song
  btnAddSong.addEventListener("click", () => {
    s.pickerSelectedScore = null;
    songSearch.value = "";
    songStart.value = 1;
    songEnd.value = 0;
    songPickerAdd.disabled = true;
    renderSongPicker("");
    songPickerDialog.showModal();
    songSearch.focus();
  });

  let songSearchTimer = null;
  songSearch.addEventListener("input", () => {
    if (songSearchTimer) clearTimeout(songSearchTimer);
    songSearchTimer = setTimeout(() => renderSongPicker(songSearch.value), 200);
  });

  songPickerCancel.addEventListener("click", () => {
    s.pickerSelectedScore = null;
    songPickerDialog.close();
  });

  songPickerDialog.addEventListener("close", () => {
    if (songPickerDialog.returnValue !== "add" || !s.pickerSelectedScore) {
      s.pickerSelectedScore = null;
      return;
    }
    const startPage = parseInt(songStart.value, 10) || 1;
    const endVal = parseInt(songEnd.value, 10) || 0;

    s.editingSetlistItems.push({
      type: "song",
      path: s.pickerSelectedScore.filepath,
      title: s.pickerSelectedScore.title,
      composer: s.pickerSelectedScore.composer,
      start_page: startPage,
      end_page: endVal === 0 ? null : endVal,
    });

    s.pickerSelectedScore = null;
    saveSetlistItems();
    renderSetlistDetail();
  });

  // Play setlist
  btnPlaySetlist.addEventListener("click", () => {
    if (s.editingSetlistItems.length === 0) return;
    startSetlistPlayback(s.editingSetlistName);
  });

  // Add setlist reference
  btnAddSetlistRef.addEventListener("click", async () => {
    if (!s.editingSetlistName) return;
    try {
      const data = await api("/api/setlists");
      setlistRefPickerList.innerHTML = "";
      const available = data.setlists.filter(
        (sl) => sl.name !== s.editingSetlistName
      );
      if (available.length === 0) {
        setlistRefPickerList.innerHTML =
          '<p style="padding:10px;color:var(--fg-dim)">No other setlists available.</p>';
      } else {
        for (const sl of available) {
          const div = document.createElement("div");
          div.className = "picker-item";
          const countLabel = sl.count === sl.flat_count
            ? `${sl.count} songs`
            : `${sl.count} items, ${sl.flat_count} songs`;
          div.textContent = `${sl.name} (${countLabel})`;
          div.addEventListener("click", () => {
            s.editingSetlistItems.push({
              type: "setlist_ref",
              setlist_name: sl.name,
            });
            setlistRefPickerDialog.close();
            saveSetlistItems();
            renderSetlistDetail();
          });
          setlistRefPickerList.appendChild(div);
        }
      }
      setlistRefPickerDialog.showModal();
    } catch (err) {
      console.error("Failed to load setlists for ref picker:", err);
    }
  });

  setlistRefPickerCancel.addEventListener("click", () =>
    setlistRefPickerDialog.close()
  );
}
