import {
  Activity, ArrowLeft, ArrowRight, AudioLines, Bot, Check, CirclePlay, CircleQuestionMark, Clock, Copy, Database, Ear,
  ExternalLink, FileText, Flag, Folder, History, Info, KeyRound, Layers, LayoutDashboard, Menu, MessageCircle, Mic, Minus,
  Moon, Phone, PhoneOff, Play, Plus, RefreshCw, Save, Search, Settings, ShieldCheck, Square, Sun, Target, TriangleAlert,
  User, Wrench, X, Zap, ChevronsUpDown, Plug, ScrollText, ListChecks, SlidersHorizontal, House, LayoutGrid, TrendingUp,
  Lock, CircleCheck, Circle, ChevronRight, Scale, Tag, Sparkles, ThumbsUp, ThumbsDown, Eye, EyeOff, Volume2, VolumeX, type LucideIcon,
} from "lucide-react";

const ICONS = {
  activity: Activity, alert: TriangleAlert, arrow: ArrowRight, back: ArrowLeft, bolt: Zap, bot: Bot, check: Check,
  clock: Clock, copy: Copy, db: Database, ear: Ear, ext: ExternalLink, file: FileText, flag: Flag, folder: Folder,
  hang: PhoneOff, help: CircleQuestionMark, history: History, info: Info, key: KeyRound, layers: Layers,
  overview: LayoutDashboard, menu: Menu, mic: Mic, minus: Minus, moon: Moon, phone: Phone, play: Play,
  "play-circle": CirclePlay, plus: Plus, refresh: RefreshCw, save: Save, search: Search, settings: Settings,
  shield: ShieldCheck, stop: Square, sun: Sun, target: Target, text: MessageCircle, tool: Wrench, user: User,
  wave: AudioLines, x: X, updown: ChevronsUpDown, plug: Plug, logs: ScrollText, plan: ListChecks, sliders: SlidersHorizontal,
  home: House, grid: LayoutGrid, trend: TrendingUp, lock: Lock, "check-circle": CircleCheck, circle: Circle, chevron: ChevronRight,
  scale: Scale, tag: Tag, sparkles: Sparkles, "thumbs-up": ThumbsUp, "thumbs-down": ThumbsDown, eye: Eye, "eye-off": EyeOff,
  speaker: Volume2, mute: VolumeX,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

export function Icon({ name, size = 16, className, strokeWidth = 2 }: { name: IconName; size?: number; className?: string; strokeWidth?: number }) {
  const C = ICONS[name];
  const filled = name === "play" || name === "stop";
  return <C size={size} strokeWidth={strokeWidth} className={className} fill={filled ? "currentColor" : "none"} aria-hidden="true" />;
}
