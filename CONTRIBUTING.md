# Contributing

This project welcomes contributions and suggestions. Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## How to contribute

1. Fork the repository and create a feature branch.
2. Set up the development environment and run the test suite:

   ```bash
   pip install -e ".[dev]"
   pytest app -q
   ```

3. Make your change, keeping the sizing constants traceable to current Microsoft Learn docs.
4. Ensure the Streamlit app still launches (`streamlit run app/ptu_streamlit_app.py`) and the tests pass.
5. Open a pull request describing the change and its motivation.

> **Note:** The PTU sizing constants and pricing in this tool are indicative. If you update them,
> cite the Microsoft Learn / Azure pricing source you verified them against.
