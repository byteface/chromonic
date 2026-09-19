"""Chromonic DOM showcase — a small issue board using the public API."""

from chromonic import App
from domonic.html import *


root = body(
    style("""
        body {
            font-family: sans-serif;
            margin: 0;
            background: #f5f5f5;
        }

        header {
            padding: 20px;
            background: #202124;
            color: white;
        }

        main {
            padding: 20px;
        }

        .toolbar {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
        }

        .stats {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }

        .stat {
            display: flex;
            gap: 4px;
            padding: 12px 18px;
            background: white;
            border: 1px solid #ddd;
        }

        .board {
            display: flex;
            gap: 20px;
            align-items: flex-start;
        }

        .column {
            width: 300px;
            background: #eee;
            padding: 12px;
        }

        .column h2 {
            margin-top: 0;
        }

        .card {
            background: white;
            border: 1px solid #ccc;
            margin-bottom: 10px;
            padding: 12px;
        }

        .card.selected {
            outline: 2px solid #333;
        }

        .card-title {
            font-weight: bold;
            margin-bottom: 10px;
        }

        .card-actions {
            display: flex;
            gap: 6px;
        }

        #inspector {
            margin-top: 20px;
            padding: 14px;
            background: #202124;
            color: #eee;
            white-space: pre-wrap;
        }

        button {
            cursor: pointer;
        }
    """),

    header(
        h1("Chromonic Board"),
        p("A browser UI controlled directly from Python."),
    ),

    main(
        div(
            input(
                _id="new-task",
                _placeholder="Create an issue..."
            ),
            button("Add issue", _id="add"),
            _class="toolbar",
        ),

        div(
            div(span("Total: "), span("0", _id="total-count"), _class="stat"),
            div(span("Open: "), span("0", _id="open-count"), _class="stat"),
            div(span("Done: "), span("0", _id="done-count"), _class="stat"),
            _class="stats",
        ),

        div(
            button("All", _class="filter", **{"data-filter": "all"}),
            button("Open", _class="filter", **{"data-filter": "open"}),
            button("Done", _class="filter", **{"data-filter": "done"}),
            _class="toolbar",
        ),

        div(
            section(
                h2("Open"),
                div(_id="open-list"),
                _class="column",
            ),
            section(
                h2("Done"),
                div(_id="done-list"),
                _class="column",
            ),
            _class="board",
        ),

        h2("DOM Inspector"),
        pre("Click a card to inspect it.", _id="inspector"),
    ),
)


app = App(root, width=900, height=700)


def cards():
    return app.document.querySelectorAll(".card")


def update_counts():
    all_cards = cards()

    total = len(all_cards)
    done = 0

    for card in all_cards:
        if card.getAttribute("data-status") == "done":
            done += 1

    app.document.querySelector("#total-count").textContent = str(total)
    app.document.querySelector("#done-count").textContent = str(done)
    app.document.querySelector("#open-count").textContent = str(total - done)


def create_card(title):
    return div(
        div(title, _class="card-title"),

        div(
            button(
                "Complete",
                _class="toggle"
            ),
            button(
                "Delete",
                _class="delete"
            ),
            _class="card-actions",
        ),

        _class="card",
        **{"data-status": "open"},
    )


@app.click("#add")
def add_issue(event):
    field = app.document.querySelector("#new-task")
    text = field.value.strip()

    if not text:
        return

    card = create_card(text)

    app.document.querySelector("#open-list").appendChild(card)

    field.value = ""

    update_counts()


@app.key("#new-task", "Enter")
def enter_issue(event):
    app.trigger("#add", "click")


@app.click(".delete")
def delete_issue(event):
    card = event.currentTarget.parentNode.parentNode
    card.remove()

    app.document.querySelector("#inspector").textContent = (
        "Card removed from DOM."
    )

    update_counts()


@app.click(".toggle")
def toggle_issue(event):
    button = event.currentTarget
    card = button.parentNode.parentNode

    status = card.getAttribute("data-status")

    if status == "open":
        card.setAttribute("data-status", "done")
        button.textContent = "Reopen"

        app.document.querySelector("#done-list").appendChild(card)

    else:
        card.setAttribute("data-status", "open")
        button.textContent = "Complete"

        app.document.querySelector("#open-list").appendChild(card)

    update_counts()


@app.click(".card")
def inspect_card(event):
    card = event.currentTarget

    # `.card` delegation matches any click inside the card, including its
    # own "Complete"/"Delete" buttons (they're descendants of `.card`) --
    # without this guard, clicking "Delete" both removes the card *and*
    # runs this handler afterwards (registered later, so it fires second),
    # which then crashes on `card.parentNode.id` below because the card no
    # longer has a parent. Let a click on the card's own actions be
    # handled only by `delete_issue`/`toggle_issue`.
    if event.target.closest(".card-actions") is not None:
        return

    for node in cards():
        node.classList.remove("selected")

    card.classList.add("selected")

    title = card.querySelector(".card-title").textContent
    status = card.getAttribute("data-status")

    app.document.querySelector("#inspector").textContent = (
        "<div class=\"card selected\">\n"
        f"  title: {title}\n"
        f"  data-status: {status}\n"
        f"  children: {len(card.children)}\n"
        f"  parent: #{card.parentNode.id}\n"
        "</div>"
    )


@app.click(".filter")
def filter_cards(event):
    requested = event.currentTarget.getAttribute("data-filter")

    for card in cards():
        status = card.getAttribute("data-status")

        if requested == "all" or status == requested:
            card.style.display = ""
        else:
            card.style.display = "none"


if __name__ == "__main__":
    # Seed a few nodes so the demo isn't empty.
    open_list = app.document.querySelector("#open-list")
    done_list = app.document.querySelector("#done-list")

    open_list.appendChild(create_card("Improve CSS grid support"))
    open_list.appendChild(create_card("Implement more WPT fixtures"))
    open_list.appendChild(create_card("Build Chromonic showcase"))

    finished = create_card("querySelector support")
    finished.setAttribute("data-status", "done")
    finished.querySelector(".toggle").textContent = "Reopen"
    done_list.appendChild(finished)

    update_counts()

    app.run()