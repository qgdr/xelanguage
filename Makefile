clean:
	find ./tests -name "*.out" -exec rm -f {} +
	find ./tests -name "*.ll" -exec rm -f {} +
	find ./tests -name "*.bc" -exec rm -f {} +
	find ./tests -name "*.ast.json" -exec rm -f {} +