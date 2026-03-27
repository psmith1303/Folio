;;; setlist-editor.el --- Edit setlists.json as org-mode tables -*- lexical-binding: t; -*-

;;; Commentary:
;;
;; Renders a setlists.json file as editable org-mode tables in an
;; ephemeral buffer.  No file is created on disk for the buffer itself.
;;
;; Requires Emacs 27+ (uses `json-parse-buffer').
;;
;; Usage:
;;   (load "/path/to/setlist-editor.el")   ; or add to load-path and require
;;   M-x setlist-edit                       ; prompts for setlists.json
;;   C-c C-s                                ; save back to JSON
;;   C-c C-q                                ; quit
;;   Tab / C-c C-c                          ; standard org table navigation / align
;;
;; Buffer layout:
;;
;;   # Setlist Editor — /path/to/setlists.json
;;   # C-c C-s to save   C-c C-q to quit
;;
;;   * Setlist Name
;;   | # | Title       | Composer | Start | End | Path          |
;;   |---+-------------+----------+-------+-----+---------------|
;;   | 1 | Amazing ... | Newton   |     1 |     | Z:/Music/...  |
;;
;; Columns:
;;   #        — decorative row number; ignored when parsing back.
;;   Title    — song title (may contain spaces and most punctuation).
;;              A title starting with ">>" denotes a setlist reference
;;              (e.g. ">>Warm-up" references the setlist named "Warm-up").
;;   Composer — composer name (blank for setlist references).
;;   Start    — start_page integer (1-based; blank for setlist references).
;;   End      — end_page integer, or blank for JSON null ("last page").
;;   Path     — full portable path to the PDF; editable (blank for refs).
;;
;; Limitation: pipe characters (|) in titles or paths will corrupt the
;; table and should be avoided.

;;; Code:

(require 'json)
(require 'org)

;;;; Buffer-local state

(defvar-local setlist-editor--source-file nil
  "Absolute path to the setlists.json file being edited in this buffer.")

;;;; Minor mode

(defvar setlist-editor-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-s") #'setlist-editor-save)
    (define-key map (kbd "C-c C-q") #'setlist-editor-quit)
    ;; Intercept C-x C-s so the org buffer is never written as a file.
    (define-key map (kbd "C-x C-s") #'setlist-editor--intercept-save)
    map)
  "Keymap for `setlist-editor-mode'.")

(define-minor-mode setlist-editor-mode
  "Minor mode for editing setlists.json as org-mode tables.

\\{setlist-editor-mode-map}"
  :lighter " SetlistEd"
  :keymap setlist-editor-mode-map)

(defun setlist-editor--intercept-save ()
  "Inform the user that C-x C-s does not write this ephemeral buffer."
  (interactive)
  (message "This buffer is ephemeral. Use C-c C-s to save to JSON, or C-c C-q to quit."))

;;;; Entry point

;;;###autoload
(defun setlist-edit (file)
  "Open FILE (a setlists.json) for editing as org-mode tables.
Each setlist becomes an org level-1 heading; each song becomes a table row.
Use \\[setlist-editor-save] to write back to JSON and \\[setlist-editor-quit] to quit."
  (interactive
   (list (read-file-name "Setlist JSON file: " nil nil t)))
  (setlist--render (expand-file-name file)))

;;;; JSON → Org

(defun setlist--render (file)
  "Read FILE as setlists.json and display it in a setlist editor buffer."
  (let* ((data   (setlist--read-json file))
         (bufname (format "*setlist: %s*" (file-name-nondirectory file)))
         (buf    (get-buffer-create bufname)))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (setlist--json-to-org data file))
      (goto-char (point-min))
      (org-mode)
      (setlist-editor-mode 1)
      ;; Align every table now so the user sees formatted columns immediately.
      (org-table-map-tables #'org-table-align t)
      (setq truncate-lines t)
      (setq setlist-editor--source-file file)
      (set-buffer-modified-p nil))
    (switch-to-buffer buf)))

(defun setlist--read-json (file)
  "Parse FILE as JSON and return an alist-based representation.
Uses `json-read' with explicit type/null bindings (available since Emacs 23).
Top-level keys become symbols; arrays become lists; JSON null becomes `:null'."
  (with-temp-buffer
    (insert-file-contents file)
    (let ((json-object-type 'alist)
          (json-array-type  'list)
          (json-null        :null)
          (json-false       :false))
      (json-read))))

(defun setlist--json-to-org (data file)
  "Insert an org-mode representation of DATA (parsed setlists.json) into the current buffer.
FILE is the source path shown in the header comment."
  (insert (format "# Setlist Editor — %s\n" file))
  (insert "# C-c C-s to save   C-c C-q to quit\n\n")
  (dolist (entry data)
    (let ((name  (if (symbolp (car entry))
                     (symbol-name (car entry))
                   (car entry)))
          (items (cdr entry)))
      (insert (format "* %s\n" name))
      (insert "| # | Title | Composer | Start | End | Path |\n")
      (insert "|---+-------+----------+-------+-----+------|\n")
      (let ((i 1))
        (dolist (item items)
          (let ((item-type (or (alist-get 'type item) "song")))
            (if (string= item-type "setlist_ref")
                ;; Setlist reference: >>Name in Title column, rest blank.
                (let ((ref-name (or (alist-get 'setlist_name item) "")))
                  (insert (format "| %d | >>%s | | | | |\n" i ref-name)))
              ;; Song item.
              (let* ((path     (or (alist-get 'path      item) ""))
                     (composer (or (alist-get 'composer  item) ""))
                     (title    (or (alist-get 'title     item) ""))
                     (start-pg (or (alist-get 'start_page item) 1))
                     (end-pg      (alist-get 'end_page   item)))
                (insert (format "| %d | %s | %s | %d | %s | %s |\n"
                                i
                                title
                                composer
                                start-pg
                                (if (numberp end-pg) (number-to-string end-pg) "")
                                path)))))
          (setq i (1+ i))))
      ;; Blank line separating setlists.
      (insert "\n"))))

;;;; Org → JSON

(defun setlist--org-to-json ()
  "Parse the current org buffer and return data suitable for JSON encoding.

Returns a list of (NAME . ITEMS-VECTOR) pairs, where NAME is a string and
ITEMS-VECTOR is a vector of alists (one per song row).  Blank End cells
become the keyword `:null' (encoded as JSON null)."
  (let (result current-name current-items)
    (save-excursion
      (goto-char (point-min))
      (while (not (eobp))
        (let ((line (buffer-substring-no-properties
                     (line-beginning-position)
                     (line-end-position))))
          (cond
           ;; Level-1 org heading → start a new setlist.
           ((string-match "^\\* \\(.+\\)" line)
            (when current-name
              ;; Flush the completed setlist as a vector (JSON array).
              (push (cons current-name (vconcat (nreverse current-items))) result))
            (setq current-name (match-string 1 line)
                  current-items nil))

           ;; Org table data row: starts with | but not |--- (hline).
           ((and current-name (string-match "^|[^-]" line))
            (let* ((raw   (split-string line "|"))
                   ;; raw = ("" " cell " " cell " ... "")
                   ;; Drop the first "" and the trailing "".
                   (cells (mapcar #'string-trim (butlast (cdr raw)))))
              ;; Cell layout: 0=# 1=Title 2=Composer 3=Start 4=End 5=Path
              ;; Skip the header row (cell 0 is the literal "#").
              (unless (string= (nth 0 cells) "#")
                (let* ((title (nth 1 cells)))
                  (if (string-prefix-p ">>" title)
                      ;; Setlist reference: >>Name
                      (let ((ref-name (string-trim (substring title 2))))
                        (push `((type         . "setlist_ref")
                                (setlist_name . ,ref-name))
                              current-items))
                    ;; Song item.
                    (let* ((composer (nth 2 cells))
                           (start-s  (nth 3 cells))
                           (end-s    (nth 4 cells))
                           (path     (nth 5 cells))
                           (start-n  (if (string= start-s "") 1
                                       (string-to-number start-s)))
                           (end-val  (if (string= end-s "")
                                         :null
                                       (string-to-number end-s))))
                      (push `((type       . "song")
                              (path       . ,path)
                              (composer   . ,composer)
                              (title      . ,title)
                              (start_page . ,start-n)
                              (end_page   . ,end-val))
                            current-items)))))))))
        (forward-line 1)))
    ;; Flush the last setlist.
    (when current-name
      (push (cons current-name (vconcat (nreverse current-items))) result))
    (nreverse result)))

;;;; Save and quit

(defun setlist-editor-save ()
  "Serialise the org tables back to JSON and write to the source file.
Reports success or failure in the minibuffer."
  (interactive)
  (unless setlist-editor--source-file
    (user-error "No source file associated with this buffer"))
  (condition-case err
      (let* ((data    (setlist--org-to-json))
             (encoded (setlist--encode-json data)))
        ;; with-temp-file creates a temp buffer, runs body, then writes to FILE.
        (with-temp-file setlist-editor--source-file
          (insert encoded))
        (set-buffer-modified-p nil)
        (message "Saved → %s" setlist-editor--source-file))
    (error
     (message "Save failed: %s" (error-message-string err)))))

(defun setlist--encode-json (data)
  "Encode DATA as a pretty-printed JSON string with a trailing newline.

DATA is a list of (NAME . ITEMS-VECTOR) pairs as returned by
`setlist--org-to-json'.  String keys are interned to symbols so that
`json-encode' produces a JSON object (not an array) at the top level."
  (let* (;; Ensure top-level keys are symbols for json-encode-alist.
         (keyed (mapcar (lambda (entry)
                          (cons (if (stringp (car entry))
                                    (intern (car entry))
                                  (car entry))
                                (cdr entry)))
                        data))
         ;; Bind pretty-print settings locally.
         (json-encoding-pretty-print t)
         (json-encoding-default-indentation "    "))
    (concat (json-encode keyed) "\n")))

(defun setlist-editor-quit ()
  "Kill the setlist editor buffer.
Prompts for confirmation when there are unsaved changes."
  (interactive)
  (when (or (not (buffer-modified-p))
            (yes-or-no-p "Unsaved changes — quit anyway? "))
    (kill-buffer (current-buffer))))

(provide 'setlist-editor)

;;; setlist-editor.el ends here
