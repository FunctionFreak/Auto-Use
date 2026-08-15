This is additional domain knowledge to work efficiently (use it wisely).

<additional_knowledge>
*First action: if the Gemini chat box is visible, hide it by clicking the Gemini toggle button at the bottom of the screen before doing anything else.*

<workflow>
Build the notebook ONE CELL AT A TIME, in this exact order:

Step 1. Cell 1: use the `input` action to write a SMALL piece of code (one logical section only, e.g., `!pip install` + imports + load data).
Step 2. Run it IMMEDIATELY after writing: send the hotkey `shift+enter` (or `ctrl+enter`) right after the `input`. Write + run is ONE combined action sequence:
    [{"type": "input", "id": <cell_id>, "value": "<your code>"}, {"type": "hotkey", "value": "shift+enter"}]
    Then wait for execution to finish.
Step 3. Check that cell's output:
    - If ERROR → read the traceback of that particular cell, rewrite ONLY that cell (`input` + `shift+enter` again), and re-run. Repeat until it passes.
    - If SUCCESS → continue to Step 4.
Step 4. Click "+ Code" to add a new cell.
Step 5. Cell 2: `input` the next small section + `shift+enter` to run → check output → fix and re-run if error.
Step 6. Click "+ Code" again → Cell 3: `input` + `shift+enter` → verify → fix.
Step 7. Repeat this one-by-one loop (input → shift+enter → check → fix → add cell) until the whole task is complete.

Rules: never write multiple cells ahead without running. Never leave a freshly written cell unexecuted. Never add a new cell while the current one is still broken.
</workflow>

1. Context and Environment
    1.1. You are operating inside a Jupyter / Google Colab notebook through the browser UI.
    1.2. All code must be written and executed inside notebook code cells — never only in the terminal.
    1.3. Colab does not have every library pre-installed. If an import is missing, install it with `!pip install <package>` (put installs at the top, ideally in the first cell) and run that cell before importing.

2. Breaking the Problem into Sections
    2.1. Never write the entire solution in one go. Split the task into small, logical sections, give each section its own cell, and apply the <workflow> above to every cell.
    2.2. Example Machine Learning pipeline:
        2.2.1. Cell 1: `!pip install ...`, imports, and load the data.
        2.2.2. Cell 2: Data cleaning and preprocessing (handling missing values, standardizing data).
        2.2.3. Cell 3: Splitting the data (train/test).
        2.2.4. Cell 4: Model training and hyperparameter tuning.
        2.2.5. Cell 5: Evaluation and results.
    2.3. If for any reason you cannot run a cell immediately after writing it, store a note in 'memory' indicating that running this cell is pending.

3. Error Diagnosis
    3.1. Explicitly determine whether the error originates from the newly written code or from a previously executed cell (e.g., a variable, dataframe, or state defined earlier).
    3.2. If the root cause is an earlier cell, fix and re-run that earlier cell first, then re-run the current cell.
    3.3. The last lines of a traceback name the error type and the failing line — always read them fully. Scroll through the output cell if the traceback is long.

4. Inserting Fresh Code into a Cell
    4.1. Use the `input` action to insert fresh code into a cell. `input` completely overrides the cell's old content and replaces it with the new code.
    4.2. ALWAYS pair `input` with an immediate run: follow it with the hotkey `shift+enter` (or `ctrl+enter`) in the same action sequence, so the cell executes right after the code is written. Never send an `input` and stop there.

5. Editing Existing Cell Code
    5.1. Complete override: if you want to completely rewrite the code in a cell, use `input` (see 4.1). It replaces all old content.
    5.2. Specific line editing: if you want to edit one line or add new lines after it:
        5.2.1. Locate the target line in the screenshot / element tree using its `OCR_TEXT` word elements.
        5.2.2. Send a double-click (left_click with clicks: 2) on the element [id] of the FIRST word of that line to place the cursor there, then press the hotkey `shift+end`. This selects the whole line.
        5.2.3. Use `typewrite` to type the replacement — it overwrites the selected line. You can also append `\n` plus extra code in the same `typewrite` to add new lines below it.
        5.2.4. Be extremely careful with indentation — Python is indentation-sensitive, so always check the indentation of the replacement line and any added lines.
        5.2.5. Example Scenario: line 5 has an error because the code incorrectly starts with "dt" instead of "df". In the image and element tree, this word is identified as `[108]<Word="dt", type="OCR_TEXT", active="True", visibility="full" />`. The action sequence to select and replace this line would be:
            5.2.5.1. [{"type": "left_click", "id": 108, "clicks": 2}, {"type": "hotkey", "value": "shift+end"}, {"type": "typewrite", "value": "df = pd.read_csv('ultimate_student_productivity_dataset_5000.csv')"}]
    5.3. Alternative Method: you may use the terminal to help edit cell content, but the final executed code must always reside inside the notebook cells, never only in terminal scripts.

6. Colab UI Reference
    6.1. Add a new code cell: click "+ Code" in the top toolbar, or hover just below the current cell and click the "+ Code" button that appears.
    6.2. Run a cell: click the round play button at the cell's left edge, or press Ctrl+Enter (run in place) / Shift+Enter (run and move to next cell — on the last cell, Shift+Enter also creates a new cell below, which can replace clicking "+ Code").
    6.3. While a cell is running, the play button becomes a spinner/stop icon — wait until execution finishes before reading the output or judging errors.
    6.4. If the runtime disconnects, click "Reconnect" / "Connect" at the top right. If a package install prompts for a restart, use Runtime > Restart session, then re-run the cells from the top in order.

7. Reading Output
    7.1. Each cell has its own output area directly below it. Scroll through it to inspect printed results, tables, plots, and full error tracebacks whenever needed.
</additional_knowledge>