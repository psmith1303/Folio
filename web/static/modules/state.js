// ---------------------------------------------------------------------------
// Centralized application state
// ---------------------------------------------------------------------------

const state = {
  // View
  currentView: "library",
  returnView: "library",

  // Library
  scores: [],
  composers: [],
  tags: [],
  selectedTags: new Set(),
  sortCol: "composer",
  sortDesc: false,

  // Viewer / PDF
  pdfDoc: null,
  currentPage: 1,
  totalPages: 0,
  currentScore: null,
  displayMode: "fit",
  userLockedMode: false,
  rendering: false,
  pageLayouts: [],
  cachedPages: new Map(),
  scrollToBottomAfterRender: false,

  // Annotations
  activeTool: "nav",
  penColor: "black",
  annotations: {},
  rotations: {},
  currentStroke: [],
  undoStacks: {},
  annotationEtag: null,
  pendingTextAnnot: null,

  // Setlists
  setlistPlayback: null,
  editingSetlistName: null,
  editingSetlistItems: [],
  setlistNameMode: "create",
  pickerSelectedScore: null,
  _pickerSelectedSetlist: null,
  _editingFilenameTags: [],
  _editingFolderTags: [],

  // Fullscreen
  pseudoFullscreen: false,
};

export function getState() {
  return state;
}

// Reset all viewer/annotation state when closing a score
export function resetViewerState() {
  state.pdfDoc = null;
  state.currentScore = null;
  state.totalPages = 0;
  state.currentPage = 1;
  state.annotations = {};
  state.rotations = {};
  state.undoStacks = {};
  state.pageLayouts = [];
  state.annotationEtag = null;
  state.setlistPlayback = null;
  state.currentStroke = [];
  state.pendingTextAnnot = null;
  state.cachedPages.clear();
}

// Reset annotation state when loading a new score (keeps viewer state)
export function resetAnnotationState() {
  state.annotations = {};
  state.rotations = {};
  state.annotationEtag = null;
  state.undoStacks = {};
  state.cachedPages.clear();
  state.userLockedMode = false;
}
