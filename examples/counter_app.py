"""Tiny public-API Chromonic app."""

from domonic.html import body, button, h1, p

from chromonic import App

root = body(
    h1("Counter"),
    button("Increment", _id="inc"),
    p("0", _id="value"),
)

app = App(root)
count = 0


@app.click("#inc")
def increment(event):
    global count
    count += 1
    app.document.querySelector("#value").textContent = str(count)


if __name__ == "__main__":
    app.run()
