import os
from .paths import prepare_environment

os.environ.update(prepare_environment(dict(os.environ)))

from .cli import main

raise SystemExit(main())
