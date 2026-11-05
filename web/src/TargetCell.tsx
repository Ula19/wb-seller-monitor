import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, rub } from "./api";

// Цель ₽: пуш «ниже цели», когда лучшая цена (витрина/бизнес/с Кошельком) опустится до неё.
// base — лучшая цена сейчас: от неё быстрые кнопки −100/−1000.
export default function TargetCell({ nm_id, target, base }: { nm_id: number; target: number | null; base: number | null }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [val, setVal] = useState("");
  const save = useMutation({
    mutationFn: (price: number | null) => price
      ? api(`/targets/${nm_id}`, { method: "PUT", body: { price } })
      : api(`/targets/${nm_id}`, { method: "DELETE" }),
    onSuccess: () => {
      setEdit(false);
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["targets"] });
    },
  });
  if (edit) {
    return (
      <form className="tgt-edit" onSubmit={(e) => {
        e.preventDefault();
        const n = parseInt(val.replace(/\D/g, ""), 10);
        if (n > 0) save.mutate(n);
      }}>
        <input autoFocus inputMode="numeric" placeholder="₽" value={val} onChange={(e) => setVal(e.target.value)}
               onKeyDown={(e) => e.key === "Escape" && setEdit(false)} />
        <button className="btn sm" disabled={save.isPending}>OK</button>
      </form>
    );
  }
  if (target) {
    return (
      <span className="tgt">
        <button className="link" title="Изменить цель" onClick={() => { setVal(String(target)); setEdit(true); }}>{rub(target)}</button>
        <button className="x" title="Снять цель" onClick={() => save.mutate(null)}>✕</button>
      </span>
    );
  }
  return (
    <span className="tgt-quick">
      {base && base > 100 && <button className="mini" title={`Цель ${rub(base - 100)}`} onClick={() => save.mutate(base - 100)}>−100</button>}
      {base && base > 1000 && <button className="mini" title={`Цель ${rub(base - 1000)}`} onClick={() => save.mutate(base - 1000)}>−1000</button>}
      <button className="mini" title="Своя цель" onClick={() => { setVal(""); setEdit(true); }}>✎</button>
    </span>
  );
}
