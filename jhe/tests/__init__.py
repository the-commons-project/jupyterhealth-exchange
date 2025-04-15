      - run: echo "Running on a ${{ runner.os }} server hosted by GitHub."
      - run: echo "Branch: ${{ github.ref }}, Repository: ${{ github.repository }}."

      - name: Check out repository code
        uses: actions/checkout@v4

      - run: echo "Repository cloned to the runner."

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.x'  # specify the desired Python version

      - name: Install dependencies using Pipenv
        run: |
          python -m pip install --upgrade pip
          pip install pipenv
          # Install both default and development dependencies (including test packages)
          pipenv install --dev

      - name: Run Django Tests
        run: |
          # Run tests with Django’s test runner
          pipenv run python manage.py test

      - name: List files in the repository (optional)
        run: ls ${{ github.workspace }}

      - run: echo 'Job status: ${{ job.status }}.'