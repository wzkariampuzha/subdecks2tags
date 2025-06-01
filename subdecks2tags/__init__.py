from aqt import mw
from aqt.qt import QAction, QInputDialog, QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QComboBox, QPushButton, QLabel, QSpinBox
from aqt.utils import showInfo, qconnect, ProgressDialog
from aqt.operations import CollectionOp, OpChanges
from anki.notes import Note
from typing import List, Tuple, Set

def get_all_deck_names() -> List[str]:
    """Retrieve all deck names sorted alphabetically."""
    return sorted([d["name"] for d in mw.col.decks.all()])

def collect_note_tag_updates(deck_name: str) -> List[Tuple[Note, Set[str]]]:
    """
    Collect notes and the tags to add based on the subdecks their cards belong to.
    Each tag corresponds to a subdeck name with spaces replaced by underscores.
    """
    print(f"Collecting tag updates for deck: {deck_name}")
    col = mw.col
    assert col.db is not None  # for type checker

    # Find all deck IDs whose names start with the specified deck_name (including subdecks)
    dids = [d["id"] for d in col.decks.all() if d["name"].startswith(deck_name)]
    if not dids:
        print("No matching decks found.")
        return []

    # Find all card IDs in those decks
    cids = col.db.list(f"SELECT id FROM cards WHERE did IN ({','.join(['?']*len(dids))})", *dids)
    print(f"Found {len(cids)} cards in selected deck and subdecks.")
    if not cids:
        return []

    # Find unique note IDs associated with those cards
    nids = set(col.db.list(f"SELECT nid FROM cards WHERE id IN ({','.join(['?']*len(cids))})", *cids))
    print(f"Corresponding to {len(nids)} unique notes.")

    updates = []
    for nid in nids:
        note = col.get_note(nid)
        tag_set = set()
        # For each card of the note, check if it belongs to one of the decks and add corresponding tag
        for card in note.cards():
            if card.did in dids:
                deck = col.decks.get(card.did)
                if deck:
                    # Replace spaces with underscores in deck name to form tag, keep '::' intact
                    tag = deck["name"].replace(" ", "_")  # keep '::'
                    tag_set.add(tag)
        if tag_set:
            updates.append((note, tag_set))
            print(f"Note {nid} → {tag_set}")
    return updates

def apply_tags_to_notes(updates: List[Tuple[Note, Set[str]]]) -> None:
    """
    Apply collected tags to the corresponding notes in the collection.
    This operation is run in the background to avoid blocking the UI.
    """
    print(f"Applying tags to {len(updates)} notes...")

    def op(col) -> OpChanges:
        # Iterate over each note and add the tags
        for note, tag_set in updates:
            for tag in tag_set:
                print(f"Adding tag '{tag}' to note {note.id}")
                note.add_tag(tag)
            print(f"Final tags for note {note.id}: {note.tags}")
            col.update_note(note)
            print(f"Updated note {note.id}")
        return OpChanges()

    # Run the operation with success and failure callbacks
    CollectionOp(parent=mw, op=op).success(
        lambda count: showInfo(f"Tagged {count} notes based on their cards' subdecks.")
    ).failure(
        lambda err: showInfo(f"Error tagging notes: {err}")
    ).run_in_background()

class SubdeckToolDialog(QDialog):
    """
    Dialog window for Subdeck Tools, allowing users to select actions such as tagging or flattening subdecks.
    """
    def __init__(self, parent, deck_names):
        super().__init__(parent)
        self.setWindowTitle("Subdeck Tools")
        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        # Informational label describing the purpose of the tool
        self.layout.addWidget(QLabel("Optimize subdecks to simplify deck hierarchy, improve performance, and avoid excessive nesting."))

        # Checkbox for adding tags based on subdecks
        self.checkbox_tag = QCheckBox("Add tags based on a card's subdecks")
        # Checkbox for flattening the deck hierarchy
        self.checkbox_flatten = QCheckBox("Flatten the deck (i.e. remove all subdecks of selected deck and reassign cards to a larger sub/deck).")
        self.layout.addWidget(self.checkbox_tag)
        self.layout.addWidget(self.checkbox_flatten)

        # Label and spinbox to select how many layers of the deck hierarchy to preserve when flattening
        self.layout.addWidget(QLabel("If flattening, preserve this many layers of the head deck's hierarchy (i.e. keep the first layer of a deck's subdecks, but reassign cards in deeper layers to the deepest remaining layer):"))
        self.level_spinbox = QSpinBox()
        self.level_spinbox.setMinimum(0)
        self.level_spinbox.setMaximum(5)
        self.level_spinbox.setValue(0)
        self.layout.addWidget(self.level_spinbox)

        # Dropdown to select the deck to operate on
        self.layout.addWidget(QLabel("Select a deck:"))
        self.deck_selector = QComboBox()
        self.deck_selector.addItems(deck_names)
        self.layout.addWidget(self.deck_selector)

        # OK and Cancel buttons
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("Optimize subdecks")
        self.ok_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel")
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        self.layout.addLayout(button_layout)

        # Connect buttons to dialog accept/reject
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        self.checkbox_tag.stateChanged.connect(self._update_ok_button_state)
        self.checkbox_flatten.stateChanged.connect(self._update_ok_button_state)

    def _update_ok_button_state(self):
        if self.checkbox_tag.isChecked() or self.checkbox_flatten.isChecked():
            self.ok_button.setEnabled(True)
        else:
            self.ok_button.setEnabled(False)

def reassign_subdeck_cards_to_head(deck_name: str, preserve_levels: int) -> None:
    """
    Flatten the deck hierarchy by reassigning cards from subdecks to higher-level decks,
    preserving a specified number of hierarchy levels.
    Empty subdecks are removed after reassignment.
    """
    print(f"Reassigning cards from subdecks of '{deck_name}' to head deck...")

    def op(col):
        head_deck_id = col.decks.id(deck_name)
        all_decks = col.decks.all()

        progress = ProgressDialog("Flattening decks...", mw)

        # Identify all subdeck IDs that start with the deck_name followed by '::'
        subdeck_ids = [d["id"] for d in all_decks if d["name"].startswith(deck_name + "::")]
        if not subdeck_ids:
            print("No subdecks found.")
            progress.close()
            return OpChanges()

        cids_to_new_did = {}
        decks_to_process = [(d["name"], d) for d in all_decks if d["name"].startswith(deck_name + "::")]
        for idx, (subdeck_name, deck) in enumerate(decks_to_process):
            progress.update_label(f"Processing deck: {subdeck_name}")
            progress.set_value(idx)
            subdeck_id = deck["id"]
            parts = subdeck_name.split("::")
            preserved_parts = parts[:preserve_levels + 1]
            new_deck_name = "::".join(preserved_parts)
            new_did = col.decks.id(new_deck_name)
            # Get all card IDs in this subdeck
            subdeck_cids = col.db.list("SELECT id FROM cards WHERE did = ?", subdeck_id)
            # Map each card ID to the new deck ID
            for cid in subdeck_cids:
                cids_to_new_did[cid] = new_did

        # Reassign cards to their new decks
        for new_did in set(cids_to_new_did.values()):
            cards = [cid for cid, did in cids_to_new_did.items() if did == new_did]
            print(f"Reassigning {len(cards)} cards to '{col.decks.get(new_did)['name']}'")
            col.set_deck(cards, new_did)

        removed_names = []
        # Refresh deck list to ensure we are checking the current state after reassignment
        updated_decks = col.decks.all()
        # Remove empty subdecks after reassignment
        for deck in updated_decks:
            if deck["name"].startswith(deck_name + "::"):
                subdeck_id = deck["id"]
                card_count = col.db.scalar("SELECT COUNT(1) FROM cards WHERE did = ?", subdeck_id)
                if card_count == 0:
                    print(f"Removing empty subdeck '{deck['name']}'")
                    col.decks.rem(subdeck_id)
                    removed_names.append(deck["name"])
        print(f"Removed {len(removed_names)} empty subdecks.")
        progress.close()
        return OpChanges()

    def on_success():
        msg = f"Moved all cards from subdecks into '{deck_name}'. Empty subdecks were removed.\n\nThis action is undoable from Edit → Undo."
        showInfo(msg)

    # Run the operation with success and failure callbacks
    CollectionOp(parent=mw, op=op).success(lambda _: on_success()).failure(
        lambda err: showInfo(f"Error flattening subdecks: {err}")
    ).run_in_background()

def show_subdeck_tool_dialog():
    """Show the Subdeck Tools dialog and execute selected operations."""
    deck_names = get_all_deck_names()
    if not deck_names:
        showInfo("No decks found.")
        return

    dialog = SubdeckToolDialog(mw, deck_names)
    if not dialog.exec():
        return

    if not dialog.checkbox_tag.isChecked() and not dialog.checkbox_flatten.isChecked() and dialog.level_spinbox.value() != 0:
        showInfo('Did you want to flatten the decks? If so, check the "Flatten the deck" box')
        return

    selected_deck = dialog.deck_selector.currentText()
    preserve_levels = dialog.level_spinbox.value()

    # Only run tagging if the checkbox is checked
    if dialog.checkbox_tag.isChecked():
        print("Tagging selected.")
        updates = collect_note_tag_updates(selected_deck)
        if updates:
            apply_tags_to_notes(updates)
        else:
            showInfo("No notes found to tag.")

    # Only run flattening if the checkbox is checked
    if dialog.checkbox_flatten.isChecked():
        print("Flattening selected.")
        reassign_subdeck_cards_to_head(selected_deck, preserve_levels)

# Update toolbar button to open the new dialog
action = QAction("Subdeck Optimization", mw)
qconnect(action.triggered, show_subdeck_tool_dialog)
mw.form.menuTools.addAction(action)