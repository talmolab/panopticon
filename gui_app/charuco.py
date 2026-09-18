"""ChArUco board construction shared by the solve and the live HUD.

One place decides which ArUco dictionary a board config names, which OpenCV
ArUco API the running build exposes, and what to do about the legacy corner
layout. The solve (``1_calibrate.py``) and the coverage detector
(``gui_app/board_detector.py``) both build their board here, so the two can
never disagree about the board they are looking for.

Rules and their reasons:

- An unknown dictionary name raises ``ValueError`` naming the valid choices.
  Falling back to a default dictionary detects nothing and reports "no board
  detections", which sends the operator to re-check the board instead of the
  config.
- ``board_legacy: true`` on a build without ``setLegacyPattern`` raises. A
  legacy-printed board detected with the current layout returns every marker
  and ZERO charuco corners, silently, so refusing is the only safe answer.
- ``cv2`` is imported inside the functions, not at module level, so importing
  this module (and ``board_detector``) costs nothing on a host without OpenCV;
  the HUD self-disables and the tests run with numpy only.
"""


def dictionary_name(cfg: dict) -> str:
    """Return the ``DICT_<bits>X<bits>_<size>`` name a board config asks for."""
    bits = int(cfg.get("marker_bits", 4))
    size = int(cfg.get("dict_size", 1000))
    return "DICT_{0}X{0}_{1}".format(bits, size)


def valid_dictionary_names() -> list[str]:
    """Every predefined dictionary name this OpenCV build defines, sorted."""
    import cv2
    return sorted(n for n in dir(cv2.aruco) if n.startswith("DICT_"))


def resolve_dictionary(cfg: dict):
    """Return the predefined ``cv2.aruco`` dictionary a board config names.

    Raises ``ValueError`` for a name this OpenCV build does not define, listing
    the valid names, because a silent default dictionary yields zero detections
    and a misleading diagnosis.
    """
    import cv2
    aruco = cv2.aruco
    name = dictionary_name(cfg)
    if not hasattr(aruco, name):
        raise ValueError(
            "unknown ArUco dictionary {} (marker_bits={}, dict_size={}); "
            "valid: {}".format(name, cfg.get("marker_bits", 4),
                               cfg.get("dict_size", 1000),
                               ", ".join(valid_dictionary_names())))
    return aruco.getPredefinedDictionary(getattr(aruco, name))


def uses_new_api() -> bool:
    """True on the >= 4.7 ArUco API (``CharucoBoard`` class, ``ArucoDetector``).

    The one version check every ArUco call site relies on, for the board
    constructor and the marker detector alike: the class constructor exists and
    the pre-4.7 factory function does not. A second heuristic beside this one
    can disagree with it on an intermediate build and pair a new-API board with
    a deprecated detection path, so none is kept.
    """
    import cv2
    aruco = cv2.aruco
    return (hasattr(aruco, "CharucoBoard")
            and not hasattr(aruco, "CharucoBoard_create"))


def apply_legacy_pattern(board, legacy: bool):
    """Opt the board into the pre-4.6 ChArUco corner layout, or refuse.

    Skipping silently when the build lacks ``setLegacyPattern`` reintroduces the
    empty-calibration failure the flag exists to prevent, so a legacy board on a
    build that cannot express it raises instead.
    """
    if not legacy:
        return
    if not hasattr(board, "setLegacyPattern"):
        import cv2
        raise RuntimeError(
            "board_legacy: true needs OpenCV >= 4.7 for setLegacyPattern, but "
            "this environment resolved {}. Pre-4.7 builds use the legacy "
            "layout natively, but that cannot be confirmed from here; pin "
            "opencv-contrib-python>=4.7 rather than guess.".format(
                cv2.__version__))
    board.setLegacyPattern(True)


def make_board(cfg: dict):
    """Build ``(board, aruco_dict)`` from a board config dict.

    Required keys: ``board_x``, ``board_y``, ``square_length``,
    ``marker_length``. Optional: ``marker_bits`` (4), ``dict_size`` (1000),
    ``board_legacy`` (False). Absolute lengths do not affect detection, only
    the solve's scale, so the HUD passes the real values to stay identical to
    the solve.
    """
    import cv2
    aruco = cv2.aruco
    aruco_dict = resolve_dictionary(cfg)
    bx, by = int(cfg["board_x"]), int(cfg["board_y"])
    sq, mk = float(cfg["square_length"]), float(cfg["marker_length"])
    if uses_new_api():
        board = aruco.CharucoBoard((bx, by), sq, mk, aruco_dict)
    else:
        board = aruco.CharucoBoard_create(bx, by, sq, mk, aruco_dict)
    apply_legacy_pattern(board, bool(cfg.get("board_legacy", False)))
    return board, aruco_dict


def make_marker_detector(aruco_dict):
    """Return ``detect(gray) -> (marker_corners, marker_ids)`` for this build.

    The API choice comes from ``uses_new_api()``, the same check that picked
    the board constructor, so the detector and the board can never come from
    different API generations. Both the solve and the HUD detect markers
    through this function: the solve interpolates charuco corners from the
    result, the HUD needs only the marker count and centroid, and the two must
    see the same markers for the HUD's READY to predict the solve's coverage.
    ``marker_ids`` is None or empty when nothing is found.
    """
    import cv2
    aruco = cv2.aruco
    if uses_new_api():
        detector = aruco.ArucoDetector(aruco_dict, aruco.DetectorParameters())

        def detect(gray):
            corners, ids, _rejected = detector.detectMarkers(gray)
            return corners, ids
    else:
        params = aruco.DetectorParameters_create()

        def detect(gray):
            corners, ids, _rejected = aruco.detectMarkers(
                gray, aruco_dict, parameters=params)
            return corners, ids
    return detect


def board_summary(cfg: dict) -> dict:
    """The board parameters worth persisting beside a calibration."""
    return {
        "board_x": int(cfg["board_x"]),
        "board_y": int(cfg["board_y"]),
        "square_length": float(cfg["square_length"]),
        "marker_length": float(cfg["marker_length"]),
        "board_legacy": bool(cfg.get("board_legacy", False)),
        "dictionary": dictionary_name(cfg),
    }
