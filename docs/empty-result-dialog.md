# Empty-result dialogs and query attribution

Recognize a visible Headless UI open dialog panel or `role="dialog"` with:

- visible h2/h3 heading `No se encontraron resultados`;
- a visible paragraph matching `No se encontraron resultados para la búsqueda de texto "...".`;
- a visible button named `Cerrar`.

The portal may display the literal string `null` for a tuple search. This is
not proof of missing input, deleted records, or an empty result for the next job.
A modal already open before submission belongs to an earlier operation. Close
only this recognized dialog and wait for it to disappear before filling the
next job. Never close the quota or compromised-route dialogs via this path.

The result authority remains the matching POST response for the exact foja,
numero and ano. An empty list is terminal and cached by the existing job path.
Never convert an uncorrelated dialog, timeout, or malformed response into an
empty result. Do not reload, replay a submitted query, rotate proxies, or clear
browser state to dismiss an empty-result dialog.

Validation includes a real isolated Chrome test where an old null dialog is
closed, the next distinct query is submitted exactly once, and its non-empty
result is preserved. Hidden and quota dialogs are not dismissed.
