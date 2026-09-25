"""Construiește FootyPreds.xlsm din FootyPreds.bas cu Excel desktop (opțional).

Necesită Windows, Microsoft Excel, pywin32 (pip install pywin32) și opțiunea
„Trust access to the VBA project object model”. Fără ele, scriptul afișează pașii
manuali și se termină cu codul 2 (fără să modifice nimic).

Utilizare (din rădăcina proiectului):
    .venv\\Scripts\\python.exe footypreds\\excel_client\\build_xlsm.py [--output CALE]
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BAS = HERE / "FootyPreds.bas"
OUTPUT = HERE / "FootyPreds.xlsm"
XL_OPEN_XML_WORKBOOK_MACRO_ENABLED = 52

EXIT_OK, EXIT_UNAVAILABLE, EXIT_TRUST, EXIT_FAILED = 0, 2, 3, 1
# Foile create de Setup și mesajul de succes scris în Panou!B15 (vezi FootyPreds.bas).
SHEETS = (
    "Panou",
    "Predictii",
    "Meci",
    "Forma",
    "ScorCorect",
    "Valoare",
    "TrackRecord",
    "Recomandari",
    "Live",
    "Simulare",
    "Portofel",
    "Ajutor",
    "Liste",
)
STATUS_CELL = "B15"
READY = "Foile sunt gata"

MANUAL = """
Pași manuali (durează un minut):
  1. Pornește serverul: dublu-click pe start.ps1 (sau PowerShell: .\\start.ps1).
  2. Deschide Excel > Registru de lucru necompletat.
  3. Alt+F11 (editorul VBA) > File > Import File... > alege
     footypreds\\excel_client\\FootyPreds.bas
  4. Închide editorul, apoi Alt+F8 > Setup > Run.
  5. Fișier > Salvare ca > tipul „Registru de lucru Excel cu macrocomenzi (*.xlsm)”.
Detalii: footypreds\\excel_client\\README.md
""".strip("\n")

TRUST = """
Excel nu permite importul automat al modulului VBA.
Activează: Fișier > Opțiuni > Centru de autorizare > Setări Centru de autorizare >
Setări macrocomenzi > bifează „Încredere în accesul la modelul de obiecte al
proiectului VBA” (Trust access to the VBA project object model), apoi rulează din nou.
Sau urmează pașii manuali de mai jos.
""".strip("\n")


class BuildError(Exception):
    def __init__(self, message, code=EXIT_FAILED):
        super().__init__(message)
        self.code = code


def open_excel():
    """Returns (excel, None) or (None, reason) when Excel automation is not available."""
    if sys.platform != "win32":
        return None, "Excel pentru Windows este necesar (sistemul curent nu este Windows)."
    try:
        import win32com.client
    except ImportError:
        return None, "Lipsește pywin32 (instalează cu: pip install pywin32)."
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
    except Exception as exc:  # pywintypes.com_error when Excel is not installed
        return None, f"Microsoft Excel nu poate fi pornit ({exc})."
    return excel, None


def check_setup(workbook):
    """Setup rulează în Excel ascuns și nu afișează MsgBox: eroarea rămâne în Panou!B15."""
    names = {sheet.Name for sheet in workbook.Worksheets}
    status = ""
    if "Panou" in names:
        status = str(workbook.Worksheets("Panou").Range(STATUS_CELL).Value or "").strip()
    missing = [name for name in SHEETS if name not in names]
    if missing or READY not in status:
        detail = status or "fără mesaj"
        if missing:
            detail += "; lipsesc foile " + ", ".join(missing)
        raise BuildError(f"Setup a eșuat în Excel: {detail}.")


def build(excel, output, bas=BAS):
    """Imports the module, runs Setup and saves an .xlsm; always closes Excel."""
    workbook = None
    try:
        excel.Visible = False
        excel.DisplayAlerts = False
        workbook = excel.Workbooks.Add()
        try:
            components = workbook.VBProject.VBComponents
        except Exception as exc:
            raise BuildError(TRUST, EXIT_TRUST) from exc
        components.Import(str(bas))
        excel.Run(f"'{workbook.Name}'!Setup")
        check_setup(workbook)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        workbook.SaveAs(str(output), FileFormat=XL_OPEN_XML_WORKBOOK_MACRO_ENABLED)
    finally:
        if workbook is not None:
            workbook.Close(SaveChanges=False)
        excel.Quit()
    return output


def main(argv=None, opener=open_excel, out=print):
    parser = argparse.ArgumentParser(description="Construiește FootyPreds.xlsm cu Excel.")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="fișierul .xlsm rezultat")
    args = parser.parse_args(argv)
    if not BAS.exists():
        out(f"Lipsește {BAS}.")
        return EXIT_FAILED
    excel, reason = opener()
    if excel is None:
        out(f"Nu pot construi automat FootyPreds.xlsm: {reason}")
        out("")
        out(MANUAL)
        return EXIT_UNAVAILABLE
    try:
        path = build(excel, args.output.resolve())
    except BuildError as exc:
        out(str(exc))
        out("")
        out(MANUAL)
        return exc.code
    except Exception as exc:
        out(f"Construirea a eșuat: {exc}")
        out("")
        out(MANUAL)
        return EXIT_FAILED
    out(f"Gata: {path}")
    out("Deschide fișierul, pornește serverul (start.ps1) și apasă „Încarcă predicțiile”.")
    return EXIT_OK


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
