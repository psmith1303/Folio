// ---------------------------------------------------------------------------
// DOM element references — single source of truth
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

// Library
export const libraryView = $("#library-view");
export const searchInput = $("#search-input");
export const composerFilter = $("#composer-filter");
export const tagBar = $("#tag-bar");
export const libraryBody = $("#library-body");
export const libraryStatus = $("#library-status");
export const btnReset = $("#btn-reset");
export const btnOffline = $("#btn-offline");

// Top bar
export const btnLibrary = $("#btn-library");
export const btnSetlists = $("#btn-setlists");
export const btnRecent = $("#btn-recent");
export const btnSetDir = $("#btn-set-dir");
export const titleDisplay = $("#title-display");
export const btnTheme = $("#btn-theme");

// Recent view
export const recentView = $("#recent-view");
export const recentBody = $("#recent-body");
export const recentStatus = $("#recent-status");

// Viewer
export const viewerView = $("#viewer");
export const btnPrev = $("#btn-prev");
export const btnNext = $("#btn-next");
export const pageInput = $("#page-input");
export const pageTotal = $("#page-total");
export const btnZoomFit = $("#btn-zoom-fit");
export const btnZoomWide = $("#btn-zoom-wide");
export const btnSideBySide = $("#btn-side-by-side");
export const pdfContainer = $("#pdf-container");
export const canvas1 = $("#pdf-canvas");
export const canvas2 = $("#pdf-canvas-2");
export const annotCanvas1 = $("#annot-canvas");
export const annotCanvas2 = $("#annot-canvas-2");
export const pageWrap1 = $("#page-wrap-1");
export const pageWrap2 = $("#page-wrap-2");
export const btnExport = $("#btn-export");
export const btnFullscreen = $("#btn-fullscreen");

// Annotation tools
export const btnNav = $("#btn-nav");
export const btnPen = $("#btn-pen");
export const btnText = $("#btn-text");
export const btnEraser = $("#btn-eraser");
export const btnUndo = $("#btn-undo");
export const sizeSlider = $("#size-slider");
export const btnRotCCW = $("#btn-rot-ccw");
export const btnRotCW = $("#btn-rot-cw");
export const btnAddToSetlist = $("#btn-add-to-setlist");
export const btnEditTags = $("#btn-edit-tags");

// Setlist view
export const setlistView = $("#setlist-view");
export const setlistBody = $("#setlist-body");
export const setlistStatus = $("#setlist-status");
export const btnNewSetlist = $("#btn-new-setlist");
export const setlistDetailActions = $("#setlist-detail-actions");
export const setlistDetailName = $("#setlist-detail-name");
export const setlistSongsBody = $("#setlist-songs-body");
export const btnRenameSetlist = $("#btn-rename-setlist");
export const btnAddSong = $("#btn-add-song");
export const btnPlaySetlist = $("#btn-play-setlist");
export const btnAddSetlistRef = $("#btn-add-setlist-ref");
export const btnCacheSetlist = $("#btn-cache-setlist");

// Dialogs — set-folder
export const dirDialog = $("#dir-dialog");
export const dirInput = $("#dir-input");
export const dirCancel = $("#dir-cancel");

// Dialogs — text annotation
export const textDialog = $("#text-dialog");
export const textDialogTitle = $("#text-dialog-title");
export const textInput = $("#text-input");
export const textFont = $("#text-font");
export const textCancel = $("#text-cancel");

// Dialogs — setlist name
export const setlistNameDialog = $("#setlist-name-dialog");
export const setlistNameDialogTitle = $("#setlist-name-dialog-title");
export const setlistNameInput = $("#setlist-name-input");
export const setlistNameCancel = $("#setlist-name-cancel");

// Dialogs — song picker
export const songPickerDialog = $("#song-picker-dialog");
export const songSearch = $("#song-search");
export const songPickerList = $("#song-picker-list");
export const songStart = $("#song-start");
export const songEnd = $("#song-end");
export const songPickerCancel = $("#song-picker-cancel");
export const songPickerAdd = $("#song-picker-add");

// Dialogs — setlist picker (add to setlist from viewer)
export const setlistPickerDialog = $("#setlist-picker-dialog");
export const setlistPickerList = $("#setlist-picker-list");
export const setlistPickerCancel = $("#setlist-picker-cancel");
export const setlistPickerStart = $("#setlist-picker-start");
export const setlistPickerEnd = $("#setlist-picker-end");
export const setlistPickerAdd = $("#setlist-picker-add");

// Dialogs — setlist-ref picker (add sub-setlist)
export const setlistRefPickerDialog = $("#setlist-ref-picker-dialog");
export const setlistRefPickerList = $("#setlist-ref-picker-list");
export const setlistRefPickerCancel = $("#setlist-ref-picker-cancel");

// Dialogs — conflict
export const conflictDialog = $("#conflict-dialog");
export const conflictReload = $("#conflict-reload");
export const conflictForce = $("#conflict-force");

// Dialogs — login
export const loginDialog = $("#login-dialog");
export const loginInput = $("#login-input");
export const loginError = $("#login-error");

// Dialogs — offline cache
export const offlineDialog = $("#offline-dialog");

// Dialogs — tag editor
export const tagEditorDialog = $("#tag-editor-dialog");
export const tagEditorChips = $("#tag-editor-chips");
export const tagEditorInput = $("#tag-editor-input");
export const tagEditorAddBtn = $("#tag-editor-add");
export const tagEditorCancel = $("#tag-editor-cancel");
