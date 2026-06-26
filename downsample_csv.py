import csv
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("csv_file")
parser.add_argument("--every", type=int, default=100)
args = parser.parse_args()

src = Path(args.csv_file)
dst = src.with_name(src.stem + f"_preview_every_{args.every}" + src.suffix)

with src.open("r", newline="") as f_in, dst.open("w", newline="") as f_out:
    reader = csv.reader(f_in)
    writer = csv.writer(f_out)

    header = next(reader)
    writer.writerow(header)

    for i, row in enumerate(reader):
        if i % args.every == 0:
            writer.writerow(row)

print(f"Wrote {dst}")