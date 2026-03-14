## VS Code/Cursor Setup Guide for Hummingbot Testing

This guide outlines how to configure VS Code or Cursor to efficiently run and debug Hummingbot tests

**I. Prerequisites:**

* **Hummingbot Repository:** You have cloned the Hummingbot repository to your local machine.
* **Conda Environment:** You have created and activated the `hummingbot` Conda environment with all necessary dependencies installed.

**II. Required Files and Configuration:**

Ensure the following files exist in your Hummingbot project directory with the specified content.

**1. `.env` (Project Root Directory):**

```
PYTHONPATH=${PYTHONPATH}:${PWD}
CONDA_ENV=hummingbot
```

* **`PYTHONPATH`**: This ensures that Python can find the Hummingbot modules within your project directory.
* **`CONDA_ENV`**: This variable can be used by other tools or scripts to identify the active Conda environment.

**2. `.vscode/settings.json` (Create this directory and file if it doesn't exist):**

```json
{
    "python.testing.pytestEnabled": true,
    // Test selection is marker-based. See pyproject.toml for registered markers.
    // Quarantined tests are auto-excluded. Do NOT add --ignore paths here.
    "python.testing.pytestArgs": [
        "test",
        "-m", "not quarantined and not live_api"
    ],
    "python.envFile": "${workspaceFolder}/.env",
    "python.terminal.activateEnvironment": true,
}
```


**3. `.vscode/launch.json` (Create this directory and file if it doesn't exist):**

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: Hummingbot",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceRoot}/bin/hummingbot.py",
            "console": "integratedTerminal"
        }
    ]
}
```

* This configuration allows you to run and debug the main Hummingbot application directly from VS Code/Cursor.

**III. Setup Steps in VS Code/Cursor:**

1.  **Open the Hummingbot Project:** Open the root directory of your cloned Hummingbot repository in VS Code or Cursor.

2.  **Select the Python Interpreter:**
    * Open the Command Palette: Press `Ctrl+Shift+P` (Windows/Linux) or `Cmd+Shift+P` (macOS).
    * Type "Python: Select Interpreter" and press Enter.
    * A list of available Python interpreters will appear. **Select the Python interpreter associated with your `hummingbot` Conda environment.** The path should typically include the name of your Conda environment.

3.  **Ensure `.env` is Loaded:** VS Code/Cursor should automatically load the `.env` file specified in `settings.json`. You can verify this by checking the Python environment variables within the IDE's terminal or debug configurations.

4.  **Fix Test Discovery (Conda Environment Issue):**
    * Open your terminal.
    * Run the following commands to create a symbolic link to work around a known Conda environment detection issue:
        ```bash
        mkdir -p ~/anaconda3/envs/hummingbot/envs
        ln -s ~/anaconda3/envs/hummingbot/ ~/anaconda3/envs/hummingbot/envs/hummingbot
        ```
        **Note:** Adjust `~/anaconda3/envs/hummingbot` to the actual path of your `hummingbot` Conda environment if it's located elsewhere.

**IV. Running Tests:**

1.  **Open the Testing View:** In the VS Code/Cursor Activity Bar (usually on the left), click on the **Testing icon** (it often looks like a flask or a beaker).

2.  **Discover Tests:** If the tests are not automatically discovered, you might see a prompt to configure testing. Ensure pytest is selected and the `test` directory is specified as the test source. VS Code/Cursor should then discover the tests based on your `settings.json`.

3.  **Run Tests:**
    * You will see a list of discovered tests in the Testing View, organized by file and test function.
    * **Run All Tests:** Click the "Run All Tests" button (usually a play icon at the top).
    * **Run Specific Tests:** You can run individual test files, test classes, or specific test functions by right-clicking on them in the Testing View and selecting "Run".

4.  **View Test Results:** The Testing View will display the status of each test (passed, failed, skipped). You can click on a failed test to see the error output and navigate to the test code.

**V. Debugging Tests:**

1.  **Set Breakpoints:** In your test files or the Hummingbot code you want to debug, click in the gutter (the space to the left of the line numbers) to set breakpoints.

2.  **Run Tests in Debug Mode:**
    * In the Testing View, right-click on the test(s) you want to debug and select "Debug".
    * VS Code/Cursor will start the debugger and stop at your breakpoints, allowing you to inspect variables, step through code, and understand the flow of execution.

**VI. Notes on Test Selection:**

* **Marker-based selection:** Tests are categorized using pytest markers defined in `pyproject.toml`. The `settings.json` configuration excludes `quarantined` and `live_api` tests by default. Quarantined directories have a `conftest.py` with `pytestmark = pytest.mark.quarantined`.
* **To run quarantined tests:** Use `pytest -m "quarantined" test/` from the command line.
* **To run live API tests:** Use `pytest -m "live_api" test/ -v` (requires API credentials).

By following these steps, you can effectively use VS Code or Cursor to run and debug Hummingbot tests, leveraging the IDE's features for a more integrated and potentially more efficient testing experience, especially when debugging is required.
