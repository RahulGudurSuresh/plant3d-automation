"""
callout_annotations -- full P&ID line-number callouts for Plant 3D orthos.

Plant 3D's "Full Line Number Callout" style prints Size-LineNumberTag-Spec
(e.g. 80-209M01-SFSA).  The P&ID -- the mother document -- names the same
pipe 217-209-080-WCG-SFSA-NN.  This package reads the ortho DWG and the P&ID
PDF, joins them on the line sequence number, and writes one callout per
visible pipe run carrying the FULL P&ID name, in the plane of each ortho
view, with a leader to the pipe.

Pipeline:  pid.py (PDF -> names)  +  ortho.py (DXF -> views, runs)
           -> match.py (join)  -> place.py (where)  -> writeback.py (DXF)
"""

__version__ = "0.1.0"
