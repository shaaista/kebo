import { createRoot } from "react-dom/client";
import ChatHarness from "./pages/ChatHarness";
import "./index.css";

createRoot(document.getElementById("chat-root")!).render(<ChatHarness />);
