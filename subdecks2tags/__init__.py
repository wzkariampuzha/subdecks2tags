import logging
import os
from aqt import mw

from aqt.qt import QAction, QInputDialog, QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QComboBox, QPushButton, QLabel, \
    QSpinBox
from aqt.utils import showInfo, qconnect
from aqt.progress import ProgressDialog
from aqt.operations import CollectionOp, OpChanges
from anki.notes import Note
from typing import List, Tuple, Set, Dict


def get_all_deck_names() -> List[str]:
    """Retrieve all deck names sorted alphabetically."""
    return sorted([d["name"] for d in mw.col.decks.all()])


def collect_note_tag_updates(deck_name: str) -> List[Tuple[Note, Set[str]]]:
    """
    Collect notes and the tags to add based on the subdecks their cards belong to.
    Each tag corresponds to a subdeck name with spaces replaced by underscores.
    """
    logging.debug(f"[collect_note_tag_updates] Collecting tag updates for deck: {deck_name}")
    col = mw.col
    assert col.db is not None  # for type checker

    # Find all deck IDs whose names start with the specified deck_name (including subdecks)
    dids = [d["id"] for d in col.decks.all() if d["name"].startswith(deck_name)]
    if not dids:
        logging.debug("[collect_note_tag_updates] No matching decks found.")
        return []

    # Find all card IDs in those decks
    cids = col.db.list(f"SELECT id FROM cards WHERE did IN ({','.join(['?'] * len(dids))})", *dids)
    logging.debug(f"[collect_note_tag_updates] Found {len(cids)} cards in selected deck and subdecks.")
    if not cids:
        return []

    # Find unique note IDs associated with those cards
    nids = set(col.db.list(f"SELECT nid FROM cards WHERE id IN ({','.join(['?'] * len(cids))})", *cids))
    logging.debug(f"[collect_note_tag_updates] Corresponding to {len(nids)} unique notes.")

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
            logging.debug(f"[collect_note_tag_updates] Note {nid} → {tag_set}")
    return updates


def apply_tags_to_notes(updates: List[Tuple[Note, Set[str]]]) -> None:
    """
    Apply collected tags to the corresponding notes in the collection.
    This operation is run in the background to avoid blocking the UI.
    """
    logging.debug(f"[apply_tags_to_notes] Applying tags to {len(updates)} notes...")

    def op(col) -> OpChanges:
        # Iterate over each note and add the tags
        for note, tag_set in updates:
            for tag in tag_set:
                logging.debug(f"[apply_tags_to_notes] Adding tag '{tag}' to note {note.id}")
                note.add_tag(tag)
            logging.debug(f"[apply_tags_to_notes] Final tags for note {note.id}: {note.tags}")
            col.update_note(note)
            logging.debug(f"[apply_tags_to_notes] Updated note {note.id}")
        logging.debug("[apply_tags_to_notes] All note updates queued.")
        logging.debug(f"[apply_tags_to_notes] Updated {len(updates)} notes total.")
        return OpChanges()

    def on_failure(err):
        logging.error(f"[apply_tags_to_notes] Error tagging notes: {err}")
        showInfo(f"Error tagging notes: {err}")

    # Run the operation with success and failure callbacks
    CollectionOp(parent=mw, op=op).success(
        lambda _: showInfo("Finished tagging notes based on their cards' subdecks.")
    ).failure(
        on_failure
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
        self.layout.addWidget(
            QLabel("Optimize subdecks to simplify deck hierarchy, improve performance, and avoid excessive nesting."))

        # Checkbox for adding tags based on subdecks
        self.checkbox_tag = QCheckBox("Add tags based on a card's subdecks")
        # Checkbox for flattening the deck hierarchy
        self.checkbox_flatten = QCheckBox(
            "Flatten the deck (i.e. remove all subdecks of selected deck and reassign cards to a larger sub/deck).")
        self.layout.addWidget(self.checkbox_tag)
        self.layout.addWidget(self.checkbox_flatten)

        # Label and spinbox to select how many layers of the deck hierarchy to preserve when flattening
        self.layout.addWidget(QLabel(
            "If flattening, preserve this many layers of the deck's hierarchy (i.e. keep the first layer of a deck's subdecks, but reassign cards in deeper layers to the deepest remaining layer):"))
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
    Fixed version of the flattening function.
    """
    print(f"[DEBUG] [reassign_subdeck_cards_to_head] Reassigning cards from subdecks of '{deck_name}' to head deck...")

    def op(col) -> OpChanges:
        print(f"[DEBUG] [op] Starting flattening operation for deck: {deck_name}, preserve_levels: {preserve_levels}")
        assert col is not None, "[ASSERTION FAILED] Collection object is None"
        head_deck_id = col.decks.id(deck_name)
        all_decks = col.decks.all()

        # Identify all subdeck IDs that start with the deck_name followed by '::'
        subdeck_ids = [d["id"] for d in all_decks if d["name"].startswith(deck_name + "::")]
        if not subdeck_ids:
            print("[DEBUG] [reassign_subdeck_cards_to_head] No subdecks found.")
            return OpChanges()

        cids_to_new_did = {}
        decks_to_process = [(d["name"], d) for d in all_decks if d["name"].startswith(deck_name + "::")]
        preserved_level_msgs: List[str] = []

        for idx, (subdeck_name, deck) in enumerate(decks_to_process):
            subdeck_id = deck["id"]
            parts = subdeck_name.split("::")
            assert isinstance(parts, list), f"[ASSERTION FAILED] Expected parts to be a list, got {type(parts)}"

            # FIXED LOGIC: Calculate subdeck depth correctly
            subdeck_depth = len(parts) - 1  # Subtract 1 because first part is the main deck

            if subdeck_depth <= preserve_levels:
                # This subdeck should be preserved - skip it
                print(
                    f"[DEBUG] [reassign_subdeck_cards_to_head] Preserving deck '{subdeck_name}' (depth {subdeck_depth} <= preserve_levels {preserve_levels})")
                continue
            else:
                # This subdeck is too deep - flatten it to the preserved level
                preserved_parts = parts[:preserve_levels + 1]  # +1 to include the main deck name
                new_deck_name = "::".join(preserved_parts)
                new_did = col.decks.id(new_deck_name)

                subdeck_cids = col.db.list("SELECT id FROM cards WHERE did = ?", subdeck_id)
                print(
                    f"[DEBUG] [reassign_subdeck_cards_to_head] Flattening '{subdeck_name}' (depth {subdeck_depth}) to '{new_deck_name}'")
                print(
                    f"[DEBUG] [reassign_subdeck_cards_to_head] Retrieved {len(subdeck_cids)} cards from subdeck '{subdeck_name}' with id {subdeck_id}")

                for cid in subdeck_cids:
                    cids_to_new_did[cid] = new_did

        # Rest of the function remains the same...
        print(
            f"[DEBUG] [reassign_subdeck_cards_to_head] Beginning reassignment to {len(set(cids_to_new_did.values()))} unique decks.")
        from anki.utils import ids2str

        # Group cards by their new deck ID and process each group
        new_did_to_cards: Dict[int, List[int]] = {}
        for cid, new_did in cids_to_new_did.items():
            if new_did not in new_did_to_cards:
                new_did_to_cards[new_did] = []
            new_did_to_cards[new_did].append(cid)

        for new_did, cards in new_did_to_cards.items():
            print(f"[DEBUG] [reassign_subdeck_cards_to_head] Preparing to reassign cards to new_did: {new_did}")
            print(f"[DEBUG] [reassign_subdeck_cards_to_head] Cards assigned to this deck: {cards}")

            if not cards:
                print(f"[DEBUG] [reassign_subdeck_cards_to_head] No cards to reassign for deck {new_did}")
                continue

            if not isinstance(cards, list) or not all(isinstance(cid, int) for cid in cards):
                print(f"[ERROR] [reassign_subdeck_cards_to_head] Cards must be a list of ints, got: {cards}")
                continue

            try:
                print(
                    f"[DEBUG] [reassign_subdeck_cards_to_head] Reassigning {len(cards)} cards to '{col.decks.get(new_did)['name']}'")
                card_ids_str = ids2str(cards)
                col.db.execute(f"UPDATE cards SET did = ? WHERE id IN {card_ids_str}", new_did)
                print(f"[DEBUG] [reassign_subdeck_cards_to_head] Successfully reassigned {len(cards)} cards")
            except Exception as e:
                print(
                    f"[ERROR] [reassign_subdeck_cards_to_head] Failed updating deck for cards: {cards} with error: {e}")
                raise

        print(f"[DEBUG] [reassign_subdeck_cards_to_head] Finished reassignment of cards.")

        # FIXED LOGIC: Only remove decks that were actually flattened
        removed_names = []
        updated_decks = col.decks.all()

        empty_subdeck_ids = []
        for deck in updated_decks:
            if deck["name"].startswith(deck_name + "::"):
                subdeck_id = deck["id"]
                parts = deck["name"].split("::")
                subdeck_depth = len(parts) - 1

                # Only consider removing decks that exceed the preserve level
                if subdeck_depth > preserve_levels:
                    card_count = col.db.scalar("SELECT COUNT(1) FROM cards WHERE did = ?", subdeck_id)
                    if card_count == 0:
                        empty_subdeck_ids.append((subdeck_id, deck["name"]))

        # Remove empty subdecks
        for subdeck_id, subdeck_name in empty_subdeck_ids:
            print(
                f"[DEBUG] [reassign_subdeck_cards_to_head] Attempting to remove empty subdeck '{subdeck_name}' with id {subdeck_id}")
            try:
                col.decks.remove([subdeck_id])
                removed_names.append(subdeck_name)
                print(f"[DEBUG] [reassign_subdeck_cards_to_head] Successfully removed subdeck '{subdeck_name}'")
            except Exception as e:
                print(
                    f"[ERROR] [reassign_subdeck_cards_to_head] Failed to remove subdeck '{subdeck_name}' (id={subdeck_id}): {e}")
                print(f"[DEBUG] [reassign_subdeck_cards_to_head] Continuing with other removals...")

        print(f"[DEBUG] [reassign_subdeck_cards_to_head] Removed {len(removed_names)} empty subdecks.")

        assert isinstance(preserved_level_msgs,
                          list), f"[ASSERTION FAILED] preserved_level_msgs must be list, got {type(preserved_level_msgs)}"
        col._preserved_level_msgs = preserved_level_msgs
        print("[DEBUG] [op] Completed all card reassignments and subdeck removals.")
        return OpChanges()

    # Success/failure callbacks remain the same...
    def on_success(result):
        print("[DEBUG] [on_success] Flattening operation succeeded.")
        msg = f"Moved all cards from subdecks into '{deck_name}'. Empty subdecks were removed.\n\nThis action is undoable from Edit → Undo."
        preserved_msgs = getattr(mw.col, "_preserved_level_msgs", [])
        if hasattr(mw.col, "_preserved_level_msgs"):
            del mw.col._preserved_level_msgs
        if preserved_msgs:
            msg += "\n\n" + preserved_msgs[0]
        showInfo(msg)

    def on_failure(err):
        print(f"[DEBUG] [on_failure] Exception object: {repr(err)}")
        print(f"[ERROR] [on_failure] Flattening subdecks failed with error: {err}")
        print(f"[ERROR] [reassign_subdeck_cards_to_head] Error flattening subdecks: {err}")
        showInfo(f"Error flattening subdecks: {err}")

    CollectionOp(parent=mw, op=op).success(on_success).failure(on_failure).run_in_background()


def show_subdeck_tool_dialog():
    """Show the Subdeck Tools dialog and execute selected operations."""
    profile_folder = mw.pm.profileFolder()
    if profile_folder is None:
        showInfo("Cannot locate profile folder for logging.")
        return
    # log_path = os.path.join(profile_folder, "subdeck_optimization.log")
    # logging.basicConfig(
    #     filename=log_path,
    #     level=logging.DEBUG,
    #     format="%(asctime)s [%(levelname)s] %(message)s",
    # )

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
        logging.debug("[show_subdeck_tool_dialog] Tagging selected.")
        updates = collect_note_tag_updates(selected_deck)
        if updates:
            apply_tags_to_notes(updates)
        else:
            showInfo("No notes found to tag.")

    # Only run flattening if the checkbox is checked
    if dialog.checkbox_flatten.isChecked():
        logging.debug("[show_subdeck_tool_dialog] Flattening selected.")
        reassign_subdeck_cards_to_head(selected_deck, preserve_levels)


# Update toolbar button to open the new dialog
action = QAction("Subdeck Optimization", mw)
qconnect(action.triggered, show_subdeck_tool_dialog)
mw.form.menuTools.addAction(action)