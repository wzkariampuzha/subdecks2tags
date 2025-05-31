from aqt import mw
from aqt.qt import QAction, QInputDialog
from aqt.utils import showInfo, qconnect
from aqt.operations import CollectionOp, OpChanges
from anki.notes import Note
from typing import List, Tuple, Set

def get_all_deck_names() -> List[str]:
    return sorted([d["name"] for d in mw.col.decks.all()])

def collect_note_tag_updates(deck_name: str) -> List[Tuple[Note, Set[str]]]:
    print(f"Collecting tag updates for deck: {deck_name}")
    col = mw.col
    assert col.db is not None  # for type checker
    dids = [d["id"] for d in col.decks.all() if d["name"].startswith(deck_name)]
    if not dids:
        print("No matching decks found.")
        return []

    cids = col.db.list(f"SELECT id FROM cards WHERE did IN ({','.join(['?']*len(dids))})", *dids)
    print(f"Found {len(cids)} cards in selected deck and subdecks.")
    if not cids:
        return []

    nids = set(col.db.list(f"SELECT nid FROM cards WHERE id IN ({','.join(['?']*len(cids))})", *cids))
    print(f"Corresponding to {len(nids)} unique notes.")

    updates = []
    for nid in nids:
        note = col.get_note(nid)
        tag_set = set()
        for card in note.cards():
            if card.did in dids:
                deck = col.decks.get(card.did)
                if deck:
                    tag = deck["name"].replace(" ", "_")  # keep '::'
                    tag_set.add(tag)
        if tag_set:
            updates.append((note, tag_set))
            print(f"Note {nid} → {tag_set}")
    return updates

def apply_tags_to_notes(updates: List[Tuple[Note, Set[str]]]) -> None:
    print(f"Applying tags to {len(updates)} notes...")

    def op(col) -> OpChanges:
        #count = 0
        for note, tag_set in updates:
            for tag in tag_set:
                print(f"Adding tag '{tag}' to note {note.id}")
                note.add_tag(tag)
            print(f"Final tags for note {note.id}: {note.tags}")
            col.update_note(note)
            print(f"Updated note {note.id}")
        #    count += 1
        return OpChanges()
        #return OpChangesWithCount(count=count)

    CollectionOp(parent=mw, op=op).success(
        lambda count: showInfo(f"Tagged {count} notes based on their cards' subdecks.")
    ).failure(
        lambda err: showInfo(f"Error tagging notes: {err}")
    ).run_in_background()

def on_tag_cards_by_deck() -> None:
    deck_names = get_all_deck_names()
    if not deck_names:
        showInfo("No decks found.")
        return
    deck_name, ok = QInputDialog.getItem(
        mw, "Select Deck", "Choose a deck:", deck_names, 0, False
    )
    if not ok or not deck_name:
        return
    print(f"Selected deck: {deck_name}")
    updates = collect_note_tag_updates(deck_name)
    if not updates:
        showInfo("No notes found to tag.")
        return
    apply_tags_to_notes(updates)

# Add button to the toolbar
action = QAction("Tag Cards by Subdecks", mw)
qconnect(action.triggered, on_tag_cards_by_deck)
mw.form.menuTools.addAction(action)