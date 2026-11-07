import subprocess, sys, os
ids = subprocess.run([sys.executable,"-m","pytest","--collect-only","-q","-p","no:cacheprovider"],capture_output=True,text=True).stdout.splitlines()
ids = [i for i in ids if i.startswith("tests/")]
print(len(ids))
