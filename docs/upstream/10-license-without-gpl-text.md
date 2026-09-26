# LICENSE.txt references the full GPL text but does not include it

**Project:** decentespresso/decaid · **File:** `LICENSE.txt` (main, checked 2026-09-26)

## What is there

`LICENSE.txt` is the 17-line notice recommended by the GPL's "How to Apply
These Terms" - program name, copyright line, the three paragraphs - and ends:

> You should have received a copy of the GNU General Public License along with
> this program. If not, see <https://www.gnu.org/licenses/>.

There is no other licence file in the repository root (no `COPYING`, no
`LICENSE` with the full text).

## Why it matters

The notice promises that a copy of the licence came with the program, and the
GPL itself asks for that (section 4: "give all recipients a copy of this
License along with the Program"). Tools that detect licences - GitHub's own
licence badge, SPDX scanners, app-store reviews - may also not recognise a
notice alone as GPL-3.0.

## Suggestion

Add the full text as `COPYING` (the GNU convention) next to the notice, or
append it to `LICENSE.txt` after the notice. The canonical text is at
<https://www.gnu.org/licenses/gpl-3.0.txt>.

For what it is worth, decentespresso-mcp does the second: notice first, then
the unmodified text fetched from gnu.org.
