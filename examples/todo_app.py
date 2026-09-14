"""Tiny TODO app using only the public Chromonic API."""

from chromonic import App
from domonic.html import *

root = body(
    h1("Tasks"),
    div(
        input(_id="new-task", _placeholder="Add task..."),
        button("Add", _id="add"),
    ),
    ul(_id="tasks"),
)

app = App(root, width=700, height=500)


@app.click("#add")
def add_task(event):
    field = app.document.querySelector("#new-task")
    text = field.value.strip()
    if not text:
        return

    app.document.querySelector("#tasks").appendChild(
        li(
            input(_type="checkbox"),
            span(text),
            button("Delete", _class="delete"),
        )
    )

    field.value = ""


@app.key("#new-task", "Enter")
def add_with_enter(event):
    app.trigger("#add", "click")


@app.click(".delete")
def delete_task(event):
    event.currentTarget.parentNode.remove()


if __name__ == "__main__":
    app.run()
